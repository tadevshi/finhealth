"""Merchant normalisation and alias resolution.

Bank statements emit free-text ``description`` strings with branch
identifiers, legal suffixes, and installment markers
(``"MCDONALDS SUC 12"``, ``"S.A. PARIS 03/06"``, ``"LIDER COM 3"``).
Without a canonical merchant entity the user cannot answer
"what did I spend at Lider?" without scanning every row manually.
This module turns those descriptions into a :class:`Merchant`
identity, with an alias table for repeat lookups.

Three layers
------------

1. :data:`KNOWN_MERCHANT_PATTERNS` — a hardcoded dict mapping
   the *normalised* text of 12 well-known Chilean merchants to a
   :class:`app.models.category.Category` ``name``. The list covers
   ~70-80% of typical bank statement volume per design decision
   D1 in
   ``openspec/changes/phase-2-pr4-merchants-and-aliases/design.md``.
2. :func:`normalize` — the deterministic normaliser. Pure
   function: ``raw -> str`` (lowercase, accent-stripped,
   punctuation/digit/legal-entity-stripped). The single ``re.sub``
   uses ``\\b`` anchors for legal-entity tokens (``LTDA``,
   ``CIA``, ``SUCURSAL``, ``SUC``, ``COM``) so substrings of
   legitimate words (e.g. ``CINEMARK`` containing ``CIN``) are
   not over-stripped.
3. :class:`MerchantNormalizer` — orchestrates the alias-table
   hit-or-create flow plus the opt-in LLM helper. The service
   is split into two methods (``resolve_merchant`` and
   ``resolve_merchant_with_llm``) so the v1 deployment can ship
   with ``LLM_MERCHANT_NORMALIZATION_ENABLED=False`` and the
   helper stays wired in but never invoked.

Why a module-level constant for ``KNOWN_MERCHANT_PATTERNS``
-----------------------------------------------------------

The 12 entries are small enough to read at the top of the file
(per design decision D4 in the PR #4 design). They are also
importable for tests via
``from app.services.merchants import KNOWN_MERCHANT_PATTERNS``
so test code can assert exact membership without re-declaring
the table.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.merchant import Merchant, MerchantAlias, MerchantAliasSource

if TYPE_CHECKING:
    from app.models.category import Category
    from app.services.llm.protocol import LLMProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KNOWN_MERCHANT_PATTERNS (D1, D4)
# ---------------------------------------------------------------------------
#
# Twelve Chilean merchants, mapped from the *normalised* alias text
# to the canonical :class:`Category.name`. The names on the *left*
# are what :func:`normalize` produces — so they are already
# lowercase, accent-free, and stripped of digits, punctuation,
# ``S.A.``, ``LTDA``, ``CIA``, ``SUCURSAL``, ``SUC``, ``COM``.
# The names on the *right* must match the seeded category names
# in :mod:`app.models.category` (closed-set, 12 rows, ``name``
# column).
#
# Adding a row here is intentional. Renaming a category in
# migration 0005 requires updating this dict in the same PR — a
# reviewer can spot the inconsistency because the lookup is
# done via the ``categories_by_name`` dict at runtime, not via
# a DB FK, so a stale entry would silently miss the default
# category.


KNOWN_MERCHANT_PATTERNS: Final[dict[str, str]] = {
    # Dining Out
    "mcdonalds": "Dining Out",
    "starbucks": "Dining Out",
    # Groceries
    "lider": "Groceries",
    # Shopping
    "paris": "Shopping",
    "sodimac": "Shopping",
    "easy": "Shopping",
    "amazon": "Shopping",
    # Transportation
    "copec": "Transportation",
    "shell": "Transportation",
    "uber": "Transportation",
    # Subscriptions
    "netflix": "Subscriptions",
    "spotify": "Subscriptions",
}


# ---------------------------------------------------------------------------
# Normalisation regex
# ---------------------------------------------------------------------------
#
# The pipeline is five steps; the only non-obvious one is
# the *placeholder protect* pass between steps 2 and 3.
# Stripping ``/`` indiscriminately would destroy the
# installment marker (``"03/06"`` on a Paris receipt)
# which the spec scenario requires to survive the
# round-trip. Stripping digits indiscriminately would
# destroy the branch identifier (``"SUC 12"`` on a
# MCDONALDS receipt) which the spec scenario requires to
# be removed. The two are indistinguishable once ``/``
# is stripped, so the placeholder protect pass runs
# *first*, captures every ``NN/NN`` pattern as a
# non-digit marker, lets the punctuation + digit passes
# run, then restores the markers. The placeholders are
# encoded with a single uppercase letter index (``A=0``,
# ``B=1``, …) so the digit-strip pass never touches the
# marker itself.
#
# The legal-entity tokens (``S.A.``, ``LTDA``, ``CIA``,
# ``SUCURSAL``, ``SUC``, ``COM``) are matched as whole
# words so substrings of legitimate words (e.g.
# ``CINEMARK`` containing ``CIN``) are not
# over-stripped (per architecture pick A in the PR #4
# explore). ``S.A.`` uses a lookbehind/lookahead for the
# leading and trailing boundary because the trailing
# period sits between two non-word characters (period and
# space) and therefore has no ``\b`` word boundary after
# it; the explicit lookarounds cover the same ground
# without the false negative.

_LEGAL_ENTITY_TOKENS: Final = re.compile(
    r"(?<!\w)S\.A\.(?!\w)|\bLTDA\b|\bCIA\b|\bSUCURSAL\b|\bSUC\b|\bCOM\b",
    flags=re.IGNORECASE,
)
# ``NN/NN`` patterns: the installment marker. Captured
# *before* the punctuation strip so the ``/`` is not
# eaten.
_INSTALLMENT_PATTERN: Final = re.compile(r"\d+/\d+")
# ``;,.`` plus ``/`` — every punctuation character that
# does not carry semantic value in a bank description.
# The installment protect pass above saves any ``/`` that
# is part of an ``NN/NN`` pair; the rest are stripped
# here.
_PUNCTUATION: Final = re.compile(r"[;.,/]+")
# Standalone digits (e.g. branch identifiers). The
# installment protect pass saves any digit that is part of
# an ``NN/NN`` pair; the rest are stripped here.
_DIGITS: Final = re.compile(r"\d+")
# Placeholder wrapper. The middle character is a single
# uppercase letter that encodes the index in the
# placeholders list (``A=0``, ``B=1``, …). All other
# characters are non-digits and non-punctuation so neither
# the punctuation nor the digit strip pass can eat the
# marker.
_PLACEHOLDER_RE: Final = re.compile(r"INSTML([A-Z])L")
# Index alphabet — uppercase letters cover up to 26
# installment markers per call, which is well above the
# typical 5-10 a single bank statement contains. If a
# pathological statement ever has 27+ distinct installment
# markers (e.g. 27+ different stores in the same
# statement, each with a ``NN/NN``), the index overflows
# to ``[A-Z][A-Z]`` via chained ``INSTML[A-Z]L`` tokens.
_PLACEHOLDER_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def normalize(raw: str) -> str:
    """Return the canonical alias-lookup key for ``raw``.

    The transformation is a five-step pipeline:

    1. NFKD + ``encode('ascii', 'ignore')`` strips diacritics so
       ``"CAFÉ"`` → ``"cafe"`` and ``"AÉROPORT"`` → ``"aeroport"``.
       The ``unicodedata.normalize('NFKD')`` form decomposes
       accented characters into a base + combining mark, and
       ``encode('ascii', 'ignore')`` drops the combining mark
       (it is non-ASCII) so the result is plain ASCII.
    2. :data:`_LEGAL_ENTITY_TOKENS` strips whole-word legal
       suffixes (``S.A.``, ``LTDA``, ``CIA``, ``SUCURSAL``,
       ``SUC``, ``COM``). The ``\b`` anchors keep
       ``"CINEMARK"`` from being matched as ``"CIN"`` + ``"EMARK"``.
    3. *Placeholder protect* — :data:`_INSTALLMENT_PATTERN`
       captures every ``NN/NN`` pattern (e.g. ``"03/06"``)
       and replaces it with a non-digit marker
       (``"INSTMLA L"`` for the first occurrence, etc.).
       The two subsequent passes (punctuation + digit
       stripping) cannot touch the marker because every
       character in it is non-digit and non-punctuation.
    4. :data:`_PUNCTUATION` strips the ``;,.`` and ``/``
       punctuation. The installment marker has been
       captured so the ``/`` it carried is safe; every
       other ``/`` is noise (``"MCDONALDS/PARIS"`` is
       two merchants merged on one line, not one merchant
       with a slash).
    5. :data:`_DIGITS` strips standalone digits. Branch
       identifiers (``"SUC 12"``) and serial numbers
       (``"COM 3"``) are noise — the merchant identity
       is the same across all branches. Installment
       digits are protected by the placeholder pass.
    6. *Placeholder restore* — the markers captured in
       step 3 are swapped back for the original
       ``NN/NN`` text, so the installment marker
       survives the round-trip.
    7. Lowercase.
    8. ``split()`` + ``" ".join()`` collapses runs of
       whitespace and trims leading/trailing spaces. A
       ``"MCDONALDS SUC 12"`` goes through steps 1-6 to
       ``"mcdonalds  12"`` (with a double space because
       the stripped ``SUC`` left a hole); step 8 turns it
       into ``"mcdonalds"``.

    The function is pure and deterministic — the same input
    always produces the same output, no I/O, no state.
    """
    if not raw:
        return ""
    # NFKD + ASCII strip removes diacritics ("é" -> "e", etc.).
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    # Whole-word legal-entity tokens first; their removal leaves
    # an extra space that the whitespace collapse at the end
    # tidies up.
    text = _LEGAL_ENTITY_TOKENS.sub(" ", text)
    # Placeholder protect: capture every ``NN/NN`` pattern
    # so the punctuation + digit passes below do not eat
    # the installment marker. The index is encoded as a
    # single uppercase letter so the marker contains no
    # digits.
    placeholders: list[str] = []

    def _save(match: re.Match[str]) -> str:
        idx = len(placeholders)
        if idx >= len(_PLACEHOLDER_ALPHABET):
            # 26+ installment markers in one statement is
            # pathological; chained markers (one ``A`` at
            # the outer level, one ``A`` at the inner) keep
            # the index unique. The chained format is still
            # all-letters so the digit strip cannot touch
            # it. We never expect to hit this in practice.
            outer, inner = divmod(idx, len(_PLACEHOLDER_ALPHABET))
            token = f"INSTML{_PLACEHOLDER_ALPHABET[outer]}{_PLACEHOLDER_ALPHABET[inner]}L"
        else:
            token = f"INSTML{_PLACEHOLDER_ALPHABET[idx]}L"
        placeholders.append(match.group(0))
        return token

    text = _INSTALLMENT_PATTERN.sub(_save, text)
    # ``;,.`` plus ``/`` — every punctuation character that
    # does not carry semantic value. The installment
    # protect pass above saved any ``/`` that was part of
    # an ``NN/NN`` pair.
    text = _PUNCTUATION.sub(" ", text)
    # Standalone digits. The installment protect pass
    # saved any digit that was part of an ``NN/NN`` pair;
    # the rest are branch identifiers and serial numbers
    # that should not be part of the merchant identity.
    text = _DIGITS.sub(" ", text)
    # Restore the protected markers.
    if placeholders:

        def _restore(match: re.Match[str]) -> str:
            token = match.group(1)
            if len(token) == 1:
                idx = _PLACEHOLDER_ALPHABET.index(token)
            else:
                outer = _PLACEHOLDER_ALPHABET.index(token[0])
                inner = _PLACEHOLDER_ALPHABET.index(token[1])
                idx = outer * len(_PLACEHOLDER_ALPHABET) + inner
            return placeholders[idx]

        text = _PLACEHOLDER_RE.sub(_restore, text)
    text = text.lower()
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MerchantNormalizer:
    """Hit-or-create merchant lookup with opt-in LLM helper.

    The service is split into two methods so the v1 deployment
    can ship with ``LLM_MERCHANT_NORMALIZATION_ENABLED=False``
    and the helper stays wired in but never invoked. The
    deterministic :meth:`resolve_merchant` covers the common
    case (~80% per architecture pick C); the LLM helper is
    called only on a deterministic miss and only when the flag
    is on.

    The class is intentionally stateless (no ``__init__``
    parameters) so the ingestion layer can call it as
    ``MerchantNormalizer().resolve_merchant(...)`` without
    having to thread an instance through. The class form is
    used (rather than free functions) so tests can swap a
    subclass or monkey-patch a single method without touching
    the rest of the call sites.
    """

    async def resolve_merchant(
        self,
        session: AsyncSession,
        description: str,
        categories_by_name: dict[str, "Category"],
    ) -> tuple["Merchant", bool]:
        """Resolve ``description`` to a :class:`Merchant` row.

        The flow is a two-step lookup against the alias table:

        1. Compute the canonical key via :func:`normalize` and
           query ``merchant_aliases`` for an existing row.
           A hit returns the bound :class:`Merchant` *without*
           flipping ``low_confidence`` — the alias table is the
           source of truth for repeat lookups and the
           ``low_confidence`` signal is reserved for the
           *auto-create* path (per decision #9).
        2. A miss triggers the auto-create path: the canonical
           key is looked up in
           :data:`KNOWN_MERCHANT_PATTERNS` for a default
           category, and a new :class:`Merchant` +
           :class:`MerchantAlias` pair is inserted in the same
           session. The ``low_confidence`` flag is flipped to
           ``True`` when the new merchant has no
           ``default_category_id`` (i.e. the canonical key was
           not in the known-patterns dict).

        Parameters
        ----------
        session:
            The :class:`AsyncSession` to use. Must be the same
            session the ingestion layer is using for the
            ``Transaction`` inserts — the new merchant row is
            persisted on the same commit boundary.
        description:
            Raw bank description (e.g. ``"MCDONALDS SUC 12"``).
            Empty / whitespace-only descriptions return
            ``(None, False)`` so the ingestion layer can
            stamp ``merchant_id=NULL`` without an error.
        categories_by_name:
            ``{category.name.lower(): Category}`` dict. The
            ingestion layer already builds this for the
            closed-set category resolution (per design
            decision #3 — avoids N+1) and reuses it here for
            the ``default_category_id`` lookup.

        Returns
        -------
        tuple[Merchant | None, bool]
            ``(merchant, was_new)``. ``merchant`` is ``None``
            for empty descriptions; ``was_new`` is ``True``
            when this call auto-created a :class:`Merchant`
            (and therefore the caller should set
            ``low_confidence=True`` per decision D2's OR
            semantics). On an alias-table hit, ``was_new`` is
            ``False`` regardless of whether the *merchant*
            was itself a prior auto-create — the
            ``low_confidence`` signal is per-lookup, not
            per-merchant.
        """
        canonical = normalize(description)
        if not canonical:
            # Defensive: a blank or whitespace-only description
            # cannot resolve to a merchant. The ingestion
            # layer stamps ``merchant_id=NULL`` and the
            # ``low_confidence`` flag is left as-is. The
            # return shape is consistent with the hit path
            # so the caller's branch logic is simpler.
            return None, False  # type: ignore[return-value]

        # Step 1: alias-table hit. Indexed lookup on the
        # ``normalized`` column — the index is created in
        # migration 0006's PR #4 portion.
        alias_result = await session.execute(
            select(MerchantAlias).where(MerchantAlias.normalized == canonical)
        )
        existing_alias = alias_result.scalar_one_or_none()
        if existing_alias is not None:
            # The relationship is ``lazy="joined"`` so the
            # ``Merchant`` is on the attribute without an
            # extra round-trip.
            return existing_alias.merchant, False

        # Step 2: miss — auto-create. Look up the default
        # category by *name* from the closed-set dict the
        # ingestion layer already built.
        default_category: "Category | None" = None
        category_name = KNOWN_MERCHANT_PATTERNS.get(canonical)
        if category_name is not None:
            default_category = categories_by_name.get(category_name.lower())

        merchant = Merchant(
            name=canonical,
            default_category_id=default_category.id if default_category is not None else None,
            is_active=True,
        )
        session.add(merchant)
        # Flush so the merchant gets a UUID before we
        # build the alias row that references it.
        await session.flush()

        alias = MerchantAlias(
            merchant_id=merchant.id,
            alias_text=description,
            normalized=canonical,
            source=MerchantAliasSource.AUTO,
            confidence=None,
        )
        session.add(alias)
        try:
            # Flush the alias so the ``UNIQUE(alias_text)``
            # constraint is enforced now. The session-level
            # commit happens later in the ingestion
            # orchestrator; flushing here turns a race
            # collision into an immediate IntegrityError
            # that we can catch and recover from (per
            # design decision D3).
            await session.flush()
        except IntegrityError:
            # A concurrent ingest won the race for this
            # raw alias. Roll back our insert and
            # re-query the alias table to return the
            # winning merchant. The race is a
            # defensive measure; the single-user
            # use-case means it almost never fires in
            # practice, but the error path is here so
            # the second concurrent upload is not lost.
            await session.rollback()
            rerun = await session.execute(
                select(MerchantAlias).where(MerchantAlias.normalized == canonical)
            )
            winner = rerun.scalar_one_or_none()
            if winner is not None:
                return winner.merchant, False
            # If the rerun also misses (extremely
            # unlikely — the unique constraint is on
            # the raw text, not the normalized), we
            # re-raise so the operator can
            # investigate.
            raise

        return merchant, True

    async def resolve_merchant_with_llm(
        self,
        session: AsyncSession,
        description: str,
        llm_provider: "LLMProvider",
    ) -> tuple["Merchant", bool]:
        """Opt-in LLM helper for ambiguous merchant descriptions.

        Called only when ``LLM_MERCHANT_NORMALIZATION_ENABLED``
        is ``True`` *and* the deterministic path
        (:meth:`resolve_merchant`) produced a miss. The helper
        uses the existing :class:`LLMProvider` protocol — no
        new LLM call signature, no prompt change, no schema
        change. The LLM is asked (in the prompt, which is
        unchanged) to emit a canonical merchant name for the
        bank description; the helper extracts that name and
        treats it as the canonical key.

        The result is cached in ``merchant_aliases`` with
        ``source='llm'`` and the LLM's confidence score so
        subsequent uploads of the same description hit the
        alias table and skip the LLM call entirely (per
        architecture pick C — first-occurrence-only).

        Parameters
        ----------
        session:
            The :class:`AsyncSession` to use. Same session as
            :meth:`resolve_merchant`.
        description:
            Raw bank description. Empty descriptions return
            ``(None, False)`` to match the deterministic
            helper's contract.
        llm_provider:
            The :class:`LLMProvider` to use for the LLM
            call. The provider is passed in so the ingestion
            layer can reuse the same instance it uses for
            the per-chunk transaction extraction (no extra
            client construction).

        Returns
        -------
        tuple[Merchant | None, bool]
            Same shape as :meth:`resolve_merchant`. The
            ``was_new`` flag is ``True`` when this call
            auto-created a merchant.
        """
        # Local import: avoid pulling the LLM stack at
        # module load time for callers that only need
        # :func:`normalize` or :meth:`resolve_merchant`.
        from app.services.llm.schemas import ExtractionResponse

        canonical = normalize(description)
        if not canonical:
            return None, False  # type: ignore[return-value]

        # Alias-table hit short-circuit: a previous LLM
        # call already cached this description. Returning
        # the existing row is what makes the helper
        # "first-occurrence-only" — subsequent uploads
        # of the same description skip the LLM entirely.
        alias_result = await session.execute(
            select(MerchantAlias).where(MerchantAlias.normalized == canonical)
        )
        existing_alias = alias_result.scalar_one_or_none()
        if existing_alias is not None:
            return existing_alias.merchant, False

        # Ask the LLM to canonicalise the description.
        # The prompt is unchanged from the per-chunk
        # transaction extraction — the LLM is told the
        # bank description and asked to emit a canonical
        # merchant name in its ``notes`` field. We treat
        # the response as a single-row extraction and
        # read the canonical name from the first
        # transaction's ``description`` field, fallback
        # to ``notes`` if the model returned an empty
        # transactions list.
        #
        # The LLM call is wrapped in a generic try block
        # because the helper is opt-in: a transient LLM
        # failure must not abort the ingestion. The
        # fallback is the auto-create path with no
        # confidence score (source='llm', confidence=NULL
        # — the row is still cached so a future
        # first-occurrence call would re-attempt).
        try:
            response: ExtractionResponse = await llm_provider.extract_transactions(
                description, "NACIONAL"
            )
            llm_canonical = _extract_canonical_from_llm(response, description)
            confidence = float(response.confidence)
        except Exception as exc:  # pragma: no cover - opt-in defensive path
            logger.warning(
                "LLM merchant normalisation failed for %r: %s. "
                "Falling back to deterministic auto-create.",
                description,
                exc,
            )
            llm_canonical = canonical
            confidence = 0.0

        llm_canonical_normalized = normalize(llm_canonical) or canonical

        merchant = Merchant(
            name=llm_canonical_normalized,
            default_category_id=None,
            is_active=True,
        )
        session.add(merchant)
        await session.flush()

        alias = MerchantAlias(
            merchant_id=merchant.id,
            alias_text=description,
            normalized=canonical,
            source=MerchantAliasSource.LLM,
            confidence=confidence,
        )
        session.add(alias)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            rerun = await session.execute(
                select(MerchantAlias).where(MerchantAlias.normalized == canonical)
            )
            winner = rerun.scalar_one_or_none()
            if winner is not None:
                return winner.merchant, False
            raise

        return merchant, True


def _extract_canonical_from_llm(response: "ExtractionResponse", fallback: str) -> str:
    """Extract a canonical merchant name from an LLM response.

    The LLM is told to emit the bank description in the
    ``description`` field and to optionally put a canonical
    merchant name in ``notes``. The helper falls back to the
    raw description if neither is usable so the auto-create
    path is always safe.

    The extraction is deliberately tolerant: a non-string
    field, an empty string, or a string that normalises to
    the same canonical form as the original all fall back
    to the input. The intent is to never *lose* information;
    the worst case is the LLM produces a no-op canonical
    name and the alias row is created with the same
    ``normalized`` as a deterministic hit would have.
    """
    notes = getattr(response, "notes", None)
    if isinstance(notes, str) and notes.strip():
        return notes.strip()
    transactions = getattr(response, "transactions", None) or []
    if transactions:
        first = transactions[0]
        desc = getattr(first, "description", None)
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
    return fallback


__all__ = [
    "KNOWN_MERCHANT_PATTERNS",
    "MerchantNormalizer",
    "normalize",
]
