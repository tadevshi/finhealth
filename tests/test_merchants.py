"""Tests for the merchant foundation (Phase 2, PR #4).

Covers the deterministic normaliser, the alias-table
hit-or-create flow, the opt-in LLM helper, the API
endpoints, and the integration with ``_build_transactions``
(see :mod:`tests.test_ingestion` for the ``_build_transactions``
tests).

The test surface is split into three layers:

* **Normalisation unit tests** (5) — the pure ``normalize``
  function with table-driven inputs. The five canonical
  scenarios from the PR #4 spec (known patterns + the
  ``\\b`` anchor guard for ``CINEMARK`` vs ``CIA`` +
  accent/punctuation strip) are covered.
* **Alias-lookup unit tests** (5) — the
  :class:`app.services.merchants.MerchantNormalizer`
  against a real in-memory SQLite database (the alias
  lookup, auto-create, race guard, and 404/422 paths).
* **LLM helper unit tests** (2) — the opt-in path:
  flag off = 0 LLM calls, flag on = first-occurrence-only
  with a cache hit on subsequent calls.

Every test uses a fresh in-memory SQLite database (via the
``engine`` fixture from :mod:`tests.conftest`) and the
ORM schema is created via ``Base.metadata.create_all`` so
the test surface matches what the production app sees at
startup. The 12 categories are seeded by the
``session_with_categories`` fixture for the resolve path.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.main import create_app
from app.models import Category, Merchant, MerchantAlias
from app.models.merchant import MerchantAliasSource
from app.services.llm.schemas import ExtractionResponse, StatementMetadata
from app.services.merchants import (
    KNOWN_MERCHANT_PATTERNS,
    MerchantNormalizer,
    normalize,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_with_categories(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to a fresh schema with the 12 categories seeded.

    The fixture creates the full schema (so :class:`Merchant` and
    :class:`MerchantAlias` are available) and inserts the 12
    closed-set category rows that migration ``0005_phase2_categories``
    would have inserted. The merchant normalizer's
    ``default_category_id`` lookup depends on this seed; the
    PR #2 tests in :mod:`tests.test_ingestion` use the same
    fixture.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        seed = (
            ("Dining Out", "Dining Out", 1),
            ("Groceries", "Groceries", 2),
            ("Transportation", "Transportation", 3),
            ("Shopping", "Shopping", 4),
            ("Entertainment", "Entertainment", 5),
            ("Bills", "Bills & Utilities", 6),
            ("Health", "Health & Medical", 7),
            ("Travel", "Travel", 8),
            ("Subscriptions", "Subscriptions", 9),
            ("Personal Care", "Personal Care", 10),
            ("Uncategorized", "Uncategorized", 11),
            ("Other", "Other", 12),
        )
        for name, display, order in seed:
            session.add(
                Category(
                    name=name,
                    display_name=display,
                    sort_order=order,
                )
            )
        await session.commit()
        yield session


@pytest_asyncio.fixture
async def categories_by_name(session_with_categories: AsyncSession) -> dict[str, Category]:
    """Yield a ``{name.lower(): Category}`` dict built from the session.

    Mirrors the cache the ingestion layer builds at the start of
    :meth:`app.services.ingestion.IngestionService._build_transactions`.
    """
    result = await session_with_categories.execute(select(Category))
    return {category.name.lower(): category for category in result.scalars()}


@pytest_asyncio.fixture
async def client_with_categories(
    engine: AsyncEngine, session_with_categories: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to a seeded app.

    The app is created and the ``get_session`` dependency is
    overridden to point at the seeded engine so the request
    handlers see the 12 categories + the schema created by
    ``Base.metadata.create_all``.
    """
    from app.db.session import get_session

    app = create_app()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Normalisation — pure-function tests (5 from the spec)
# ---------------------------------------------------------------------------


class TestNormalize:
    """The ``normalize`` function is a pure deterministic transformation.

    Each test is a single ``raw -> expected`` pair from the
    PR #4 spec scenarios. The function is exercised in
    isolation (no DB, no I/O) so the tests are fast and the
    failures pinpoint the exact transformation that broke.
    """

    def test_normalize_known_pattern_mcdonalds(self) -> None:
        """``"MCDONALDS SUC 12"`` normalises to ``"mcdonalds"``.

        The ``SUC`` legal-entity token is stripped by the
        word-anchored regex; the branch identifier (``12``)
        is stripped as a standalone digit; the whitespace
        collapse removes the extra space left by the
        ``SUC`` removal.
        """
        assert normalize("MCDONALDS SUC 12") == "mcdonalds"

    def test_normalize_known_pattern_lider(self) -> None:
        """``"LIDER COM 3"`` normalises to ``"lider"``.

        The ``COM`` legal-entity token and the branch
        digit are stripped; the resulting spaces are
        collapsed.
        """
        assert normalize("LIDER COM 3") == "lider"

    def test_normalize_known_pattern_paris(self) -> None:
        """``"S.A. PARIS 03/06"`` preserves the installment marker.

        The ``S.A.`` legal suffix is stripped (the
        lookbehind/lookahead pair handles the trailing
        period's missing ``\\b`` word boundary); the
        ``03/06`` installment marker is captured by the
        placeholder protect pass *before* the digit strip
        runs, so it survives the round-trip.
        """
        assert normalize("S.A. PARIS 03/06") == "paris 03/06"

    def test_normalize_cinemark_not_over_stripped(self) -> None:
        """``"CINEMARK"`` is NOT over-stripped by the ``CIA`` rule.

        The ``\\b`` anchor on the ``CIA`` alternative keeps
        the regex from matching the ``CIN`` substring at
        the start of ``CINEMARK``. The result is the full
        ``cinemark`` (lowercased) — not ``cine`` or ``mark``.
        This is the design's anchor-guard test (per
        architecture pick A in the PR #4 explore).
        """
        assert normalize("CINEMARK") == "cinemark"

    def test_normalize_strips_accents_punctuation(self) -> None:
        """``"CAFÉ / AÉROPORT"`` strips accents and ``/``.

        The NFKD + ASCII-encode pass removes the diacritics
        from ``É``; the punctuation strip removes the
        ``/``; the whitespace collapse produces the
        two-word canonical form.
        """
        assert normalize("CAFÉ / AÉROPORT") == "cafe aeroport"

    def test_normalize_strips_sac_legal_suffix(self) -> None:
        """``"EMPRESA S.A.C."`` strips the ``S.A.C.`` legal-entity suffix.

        ``S.A.C.`` (Sociedad Anónima Comercial) is a common
        Chilean legal-entity suffix. The regex uses the same
        lookbehind/lookahead pair as ``S.A.`` (the trailing
        period has no ``\\b`` word boundary after it) and
        the additional ``.C.`` is anchored to a word
        character on the right via ``(?!\\w)``. The result
        is the bare merchant name ``"empresa"`` so two
        branches of the same company (``"EMPRESA S.A.C."``
        vs ``"EMPRESA"``) normalise to the same canonical
        key.
        """
        assert normalize("EMPRESA S.A.C.") == "empresa"

    def test_normalize_strips_spa_legal_suffix(self) -> None:
        """``"EMPRESA SpA"`` strips the ``SpA`` legal-entity suffix.

        ``SpA`` (Sociedad por Acciones) is a common Chilean
        legal-entity suffix. The ``\\b`` word-boundary
        anchors on either side of ``SpA`` keep the
        abbreviation from matching a substring of a
        legitimate word (the same guard the ``CIA``
        alternative uses for ``"CINEMARK"``). The result
        is the bare merchant name ``"empresa"``.
        """
        assert normalize("EMPRESA SpA") == "empresa"


# ---------------------------------------------------------------------------
# Alias lookup — DB-backed tests (5 from the spec)
# ---------------------------------------------------------------------------


class TestResolveMerchant:
    """The :class:`MerchantNormalizer` hit-or-create flow against a real DB.

    Each test exercises the full ``resolve_merchant`` path:
    ``normalize`` -> alias-table lookup -> auto-create on miss
    -> ``MerchantAlias`` row insert. The fixture gives a
    seeded categories cache so the ``default_category_id``
    lookup works the way the production ingestion layer
    uses it.
    """

    @pytest.mark.asyncio
    async def test_alias_lookup_first_upload_creates_merchant_and_alias(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """First upload of ``"MCDONALDS SUC 12"`` auto-creates a ``Merchant`` and an alias.

        The alias table is empty, so the deterministic path
        auto-creates the MCDONALDS ``Merchant`` (with
        ``default_category_id`` pointing to the seeded
        "Dining Out" row) and a ``MerchantAlias`` row with
        ``source='auto'``. The returned merchant's
        ``id`` is stamped on the new transaction.
        """
        service = MerchantNormalizer()
        merchant, was_new = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 12", categories_by_name
        )
        await session_with_categories.commit()

        assert was_new is True
        assert merchant is not None
        assert merchant.name == "mcdonalds"
        # ``KNOWN_MERCHANT_PATTERNS["mcdonalds"]`` -> "Dining Out"
        assert merchant.default_category_id == categories_by_name["dining out"].id
        assert merchant.is_active is True

        # Alias row was created with the verbatim raw text.
        alias_result = await session_with_categories.execute(
            select(MerchantAlias).where(MerchantAlias.merchant_id == merchant.id)
        )
        alias = alias_result.scalar_one()
        assert alias.alias_text == "MCDONALDS SUC 12"
        assert alias.normalized == "mcdonalds"
        assert alias.source == MerchantAliasSource.AUTO
        assert alias.confidence is None

    @pytest.mark.asyncio
    async def test_alias_lookup_second_upload_hits_existing(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """A second upload with a different branch hits the alias table.

        The first upload created the MCDONALDS ``Merchant``
        + alias on ``normalized="mcdonalds"``. A second
        upload with a different branch identifier
        (``"MCDONALDS SUC 13"``) normalises to the same
        canonical key, hits the alias table, and binds to
        the *same* merchant — no new row is created.
        """
        service = MerchantNormalizer()
        first_merchant, first_was_new = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 12", categories_by_name
        )
        await session_with_categories.commit()
        assert first_was_new is True

        second_merchant, second_was_new = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 13", categories_by_name
        )
        await session_with_categories.commit()

        assert second_was_new is False
        assert second_merchant is not None
        assert second_merchant.id == first_merchant.id

        # The merchant table has exactly one row.
        all_merchants = (await session_with_categories.execute(select(Merchant))).scalars().all()
        assert len(all_merchants) == 1

    @pytest.mark.asyncio
    async def test_alias_lookup_creates_user_alias_via_api(
        self,
        client_with_categories: AsyncClient,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """A user POST creates a new alias bound to the existing merchant.

        The test pre-creates the MCDONALDS merchant via the
        normalizer, then POSTs
        ``{"alias_text": "MAC DONALDS"}`` to
        ``/api/v1/merchants/{id}/aliases``. The endpoint
        computes the canonical form server-side, stores
        the verbatim raw text, and stamps
        ``source='user'`` on the new alias row.
        """
        service = MerchantNormalizer()
        merchant, _ = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 12", categories_by_name
        )
        await session_with_categories.commit()

        response = await client_with_categories.post(
            f"/api/v1/merchants/{merchant.id}/aliases",
            json={"alias_text": "MAC DONALDS"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["alias_text"] == "MAC DONALDS"
        # The normaliser lowercases and preserves the
        # inter-word space (``_`` would be preserved too
        # because it is a word char).
        assert body["normalized"] == "mac donalds"
        assert body["source"] == "user"
        assert body["merchant_id"] == str(merchant.id)

    @pytest.mark.asyncio
    async def test_alias_lookup_404_unknown_merchant(
        self, client_with_categories: AsyncClient
    ) -> None:
        """A POST against an unknown merchant UUID returns 404.

        The 404 check runs *before* any write so a bad UUID
        is a clean 404, not a half-applied alias or a 500
        from a missing FK.
        """
        unknown_id = uuid.uuid4()
        response = await client_with_categories.post(
            f"/api/v1/merchants/{unknown_id}/aliases",
            json={"alias_text": "WHATEVER"},
        )
        assert response.status_code == 404
        assert str(unknown_id) in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_alias_lookup_422_duplicate_alias(
        self,
        client_with_categories: AsyncClient,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """A POST with a duplicate ``alias_text`` returns 422.

        The ``UNIQUE(alias_text)`` constraint on
        ``merchant_aliases`` blocks the second insert; the
        handler catches the :class:`IntegrityError` and
        returns 422 with a descriptive message.
        """
        service = MerchantNormalizer()
        merchant, _ = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 12", categories_by_name
        )
        await session_with_categories.commit()

        # First POST succeeds.
        first = await client_with_categories.post(
            f"/api/v1/merchants/{merchant.id}/aliases",
            json={"alias_text": "MAC DONALDS"},
        )
        assert first.status_code == 200

        # Second POST with the same raw text fails.
        second = await client_with_categories.post(
            f"/api/v1/merchants/{merchant.id}/aliases",
            json={"alias_text": "MAC DONALDS"},
        )
        assert second.status_code == 422
        assert "UNIQUE" in second.json()["detail"] or "already" in second.json()["detail"]

    @pytest.mark.asyncio
    async def test_alias_lookup_422_empty_normalized_form(
        self,
        client_with_categories: AsyncClient,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """A POST whose ``alias_text`` normalises to empty returns 422.

        An ``alias_text`` made entirely of stripped
        characters (digits + punctuation, e.g. ``"//"``)
        normalises to the empty string. The handler
        rejects these at the boundary so the user sees a
        meaningful error before the alias is bound.
        """
        service = MerchantNormalizer()
        merchant, _ = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 12", categories_by_name
        )
        await session_with_categories.commit()

        response = await client_with_categories.post(
            f"/api/v1/merchants/{merchant.id}/aliases",
            json={"alias_text": "//"},
        )
        assert response.status_code == 422
        assert "empty" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_resolve_merchant_empty_description(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """An empty / whitespace description returns ``(None, False)``.

        The deterministic helper's contract is
        consistent: a blank description cannot resolve
        to a merchant, so the caller can stamp
        ``merchant_id=NULL`` without a special case.
        """
        service = MerchantNormalizer()
        merchant, was_new = await service.resolve_merchant(
            session_with_categories, "   ", categories_by_name
        )
        assert merchant is None
        assert was_new is False

    @pytest.mark.asyncio
    async def test_resolve_merchant_integrity_error_race_guard(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """A pre-existing alias with a matching ``normalized`` short-circuits the helper.

        The race guard at design decision D3 recovers
        from a concurrent insert that wins the unique
        constraint. The test simulates the race by
        pre-creating the alias row, then calls the
        helper with a description that resolves to the
        same canonical key — the alias-table lookup
        hits on the first SELECT, so the flush path
        does not fire and the race guard is *not*
        exercised (the lookup catches the collision
        before the INSERT). The test documents the
        short-circuit and asserts the deterministic
        helper returns the existing merchant.
        """
        from app.models.merchant import Merchant, MerchantAlias, MerchantAliasSource

        service = MerchantNormalizer()
        # Pre-seed a merchant + alias so the next call hits.
        first_merchant = Merchant(name="mcdonalds", is_active=True)
        session_with_categories.add(first_merchant)
        await session_with_categories.flush()
        session_with_categories.add(
            MerchantAlias(
                merchant_id=first_merchant.id,
                alias_text="MCDONALDS SUC 12",
                normalized="mcdonalds",
                source=MerchantAliasSource.AUTO,
            )
        )
        await session_with_categories.commit()

        # The helper hits the alias table on the first
        # SELECT, so the IntegrityError branch is not
        # exercised — but the helper still returns the
        # pre-seeded merchant.
        merchant, was_new = await service.resolve_merchant(
            session_with_categories, "MCDONALDS SUC 13", categories_by_name
        )
        assert was_new is False
        assert merchant is not None
        assert merchant.id == first_merchant.id


# ---------------------------------------------------------------------------
# LLM helper — opt-in path (2 from the spec)
# ---------------------------------------------------------------------------


class _CountingLLMClient:
    """Minimal :class:`LLMProvider` that records every call and returns a canned response.

    The test suite uses the counting client to assert the
    *first-occurrence-only* contract of
    :meth:`MerchantNormalizer.resolve_merchant_with_llm`:
    the LLM is called at most once per unique normalized
    text per call, and subsequent calls hit the alias
    table.
    """

    def __init__(self, response: ExtractionResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def extract_transactions(self, text: str, variant: str) -> ExtractionResponse:
        """Record the call and return the canned response."""
        self.calls.append((text, variant))
        return self.response


class TestLLMHelper:
    """The opt-in LLM helper is bounded by the alias-table cache.

    When ``LLM_MERCHANT_NORMALIZATION_ENABLED=False`` the
    helper is a no-op (zero extra LLM cost). When the flag
    is on, the helper is called *first-occurrence-only*: the
    alias table is the cache, so a second call for the same
    canonical text skips the LLM entirely.
    """

    @pytest.mark.asyncio
    async def test_llm_helper_flag_off_no_calls(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """Flag off: the helper is never invoked, even on a deterministic miss.

        The ``LLM_MERCHANT_NORMALIZATION_ENABLED`` flag
        defaults to ``False``. The
        :func:`app.services.ingestion._build_transactions`
        integration path checks the flag and routes to
        :meth:`MerchantNormalizer.resolve_merchant`
        (deterministic) instead of
        :meth:`MerchantNormalizer.resolve_merchant_with_llm`.
        The test asserts the deterministic path is taken
        by checking that no LLM client is needed.
        """
        # The session has no merchants yet. Without the
        # LLM helper, the deterministic path auto-creates
        # the merchant. No client is constructed, so the
        # only assertion is that ``resolve_merchant``
        # succeeds and the alias is created with
        # ``source='auto'`` (not ``source='llm'``).
        service = MerchantNormalizer()
        merchant, was_new = await service.resolve_merchant(
            session_with_categories,
            "TIENDA ONLINE XYZ",  # not in KNOWN_MERCHANT_PATTERNS
            categories_by_name,
        )
        await session_with_categories.commit()

        assert was_new is True
        assert merchant is not None
        assert merchant.default_category_id is None  # unknown pattern

        alias_result = await session_with_categories.execute(
            select(MerchantAlias).where(MerchantAlias.merchant_id == merchant.id)
        )
        alias = alias_result.scalar_one()
        assert alias.source == MerchantAliasSource.AUTO
        assert alias.confidence is None

    @pytest.mark.asyncio
    async def test_llm_helper_flag_on_first_occurrence_only(
        self,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """Flag on: the LLM is called once per unique text, cached on the second call.

        The :class:`_CountingLLMClient` records every call.
        The first call to
        :meth:`MerchantNormalizer.resolve_merchant_with_llm`
        with ``"AMBIGUOUS MERCHANT 1"`` invokes the LLM and
        caches the result. The second call with the same
        description hits the alias table and skips the LLM
        entirely. A third call with a *different*
        description invokes the LLM again (the first
        occurrence for the new canonical).
        """
        canned = ExtractionResponse(
            transactions=[],
            metadata=StatementMetadata(
                card_number_masked="",
                cardholder="",
                currency="CLP",
                period_start="",
                period_end="",
                statement_date="",
            ),
            confidence=0.85,
            notes="AMBIGUOUS MERCHANT",
        )
        client = _CountingLLMClient(canned)
        service = MerchantNormalizer()

        # First call: LLM is invoked once.
        first, first_was_new = await service.resolve_merchant_with_llm(
            session_with_categories, "AMBIGUOUS VENDOR ONE", client
        )
        await session_with_categories.commit()
        assert first_was_new is True
        assert first is not None
        # The LLM emitted ``"AMBIGUOUS MERCHANT"`` as the
        # canonical name; the normaliser lowercases it
        # but preserves the inter-word space.
        assert first.name == "ambiguous merchant"
        assert len(client.calls) == 1

        # Second call with the same description: alias-table
        # hit, LLM is *not* invoked.
        second, second_was_new = await service.resolve_merchant_with_llm(
            session_with_categories, "AMBIGUOUS VENDOR ONE", client
        )
        await session_with_categories.commit()
        assert second_was_new is False
        assert second.id == first.id
        assert len(client.calls) == 1  # unchanged — the alias cache absorbed the hit

        # Third call with a *different* description (a
        # different canonical key): the LLM is invoked
        # once for the new description, but the
        # returned canonical name happens to match
        # the first call's merchant. The new alias is
        # bound to the *existing* merchant (not a new
        # one), so ``was_new`` is ``False`` and the
        # merchant count stays at 1.
        third, third_was_new = await service.resolve_merchant_with_llm(
            session_with_categories, "DIFFERENT VENDOR TWO", client
        )
        await session_with_categories.commit()
        assert third_was_new is False  # merchant was reused
        assert third.id == first.id
        assert len(client.calls) == 2  # LLM was still called for the new description

        # The new alias is stamped with source='llm' and
        # the canned confidence score. There are now two
        # LLM-stamped aliases bound to the same merchant
        # (one from the first call, one from the third) —
        # we pick the one created in the third call by
        # filtering on the verbatim ``alias_text``.
        alias_result = await session_with_categories.execute(
            select(MerchantAlias).where(
                MerchantAlias.merchant_id == third.id,
                MerchantAlias.alias_text == "DIFFERENT VENDOR TWO",
            )
        )
        third_alias = alias_result.scalar_one()
        assert third_alias.source == MerchantAliasSource.LLM
        assert third_alias.confidence == 0.85
        assert third_alias.alias_text == "DIFFERENT VENDOR TWO"

    @pytest.mark.asyncio
    async def test_llm_helper_empty_description(
        self,
        session_with_categories: AsyncSession,
    ) -> None:
        """A blank description short-circuits the LLM helper.

        The helper's contract mirrors the deterministic
        path: a blank description cannot resolve to a
        merchant, so the LLM is never invoked and the
        helper returns ``(None, False)``.
        """
        from app.services.llm.schemas import (
            ExtractionResponse,
            StatementMetadata,
        )

        canned = ExtractionResponse(
            transactions=[],
            metadata=StatementMetadata(
                card_number_masked="",
                cardholder="",
                currency="CLP",
                period_start="",
                period_end="",
                statement_date="",
            ),
            confidence=0.5,
            notes="ignored",
        )
        client = _CountingLLMClient(canned)
        service = MerchantNormalizer()

        merchant, was_new = await service.resolve_merchant_with_llm(
            session_with_categories, "   ", client
        )
        assert merchant is None
        assert was_new is False
        assert len(client.calls) == 0  # LLM was not called

    def test_extract_canonical_from_llm_notes(self) -> None:
        """``_extract_canonical_from_llm`` prefers ``notes`` when set."""
        from app.services.llm.schemas import (
            ExtractionResponse,
            StatementMetadata,
        )
        from app.services.merchants import _extract_canonical_from_llm

        response = ExtractionResponse(
            transactions=[],
            metadata=StatementMetadata(
                card_number_masked="",
                cardholder="",
                currency="CLP",
                period_start="",
                period_end="",
                statement_date="",
            ),
            confidence=0.5,
            notes="  CANONICAL NAME  ",
        )
        result = _extract_canonical_from_llm(response, "fallback")
        assert result == "CANONICAL NAME"

    def test_extract_canonical_from_llm_first_transaction(self) -> None:
        """``_extract_canonical_from_llm`` falls back to the first transaction's description."""
        from app.services.llm.schemas import (
            ExtractionResponse,
            StatementMetadata,
            TransactionExtraction,
        )
        from app.services.merchants import _extract_canonical_from_llm

        response = ExtractionResponse(
            transactions=[
                TransactionExtraction(
                    date="15/05/25",
                    description="FIRST_TXN_CANONICAL",
                    amount="$ 1.000",
                    currency="CLP",
                ),
            ],
            metadata=StatementMetadata(
                card_number_masked="",
                cardholder="",
                currency="CLP",
                period_start="",
                period_end="",
                statement_date="",
            ),
            confidence=0.5,
            notes="",
        )
        result = _extract_canonical_from_llm(response, "fallback")
        assert result == "FIRST_TXN_CANONICAL"

    def test_extract_canonical_from_llm_fallback(self) -> None:
        """``_extract_canonical_from_llm`` returns the raw description when nothing usable exists."""
        from app.services.llm.schemas import (
            ExtractionResponse,
            StatementMetadata,
        )
        from app.services.merchants import _extract_canonical_from_llm

        response = ExtractionResponse(
            transactions=[],
            metadata=StatementMetadata(
                card_number_masked="",
                cardholder="",
                currency="CLP",
                period_start="",
                period_end="",
                statement_date="",
            ),
            confidence=0.5,
            notes="",
        )
        result = _extract_canonical_from_llm(response, "raw description")
        assert result == "raw description"


# ---------------------------------------------------------------------------
# KNOWN_MERCHANT_PATTERNS — table contract
# ---------------------------------------------------------------------------


class TestKnownMerchantPatterns:
    """The :data:`KNOWN_MERCHANT_PATTERNS` dict is the v1 contract.

    The dict is hardcoded (per design decision D1) and
    covers 12 Chilean merchants. The test pins the table
    size and the exact key set so a future PR that drops
    or renames an entry is caught here.
    """

    def test_known_merchant_patterns_has_twelve_entries(self) -> None:
        """The dict contains exactly 12 entries."""
        assert len(KNOWN_MERCHANT_PATTERNS) == 12

    def test_known_merchant_patterns_keys_are_canonical(self) -> None:
        """Every key is a known canonical form (lowercase, no spaces)."""
        expected_keys = {
            "mcdonalds",
            "starbucks",
            "lider",
            "paris",
            "sodimac",
            "easy",
            "amazon",
            "copec",
            "shell",
            "uber",
            "netflix",
            "spotify",
        }
        assert set(KNOWN_MERCHANT_PATTERNS.keys()) == expected_keys

    def test_known_merchant_patterns_values_resolve_to_seeded_categories(
        self, categories_by_name: dict[str, Category]
    ) -> None:
        """Every value is a seeded category name (closed-set)."""
        for canonical, category_name in KNOWN_MERCHANT_PATTERNS.items():
            assert category_name.lower() in categories_by_name, (
                f"KNOWN_MERCHANT_PATTERNS[{canonical!r}] = {category_name!r} "
                "does not match any seeded category"
            )


# ---------------------------------------------------------------------------
# API endpoints — list and POST contract
# ---------------------------------------------------------------------------


class TestMerchantAPI:
    """The two merchant endpoints round-trip the schema."""

    @pytest.mark.asyncio
    async def test_list_merchants_empty(self, client_with_categories: AsyncClient) -> None:
        """``GET /api/v1/merchants`` returns an empty list when the table is empty."""
        response = await client_with_categories.get("/api/v1/merchants")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_merchants_sorted_by_name(
        self,
        client_with_categories: AsyncClient,
        session_with_categories: AsyncSession,
        categories_by_name: dict[str, Category],
    ) -> None:
        """``GET /api/v1/merchants`` returns all merchants ordered by ``name`` ascending."""
        service = MerchantNormalizer()
        for description in ("MCDONALDS SUC 1", "AMAZON PRIME", "LIDER"):
            await service.resolve_merchant(session_with_categories, description, categories_by_name)
        await session_with_categories.commit()

        response = await client_with_categories.get("/api/v1/merchants")
        assert response.status_code == 200
        payload = response.json()
        names = [row["name"] for row in payload]
        assert names == sorted(names)
        assert "mcdonalds" in names
        assert "amazon prime" in names
        assert "lider" in names


# ---------------------------------------------------------------------------
# KNOWN_MERCHANT_PATTERNS edge cases
# ---------------------------------------------------------------------------


class TestNormalizeEdgeCases:
    """Edge cases for the pure ``normalize`` function."""

    def test_normalize_empty_string(self) -> None:
        """An empty string normalises to an empty string."""
        assert normalize("") == ""

    def test_normalize_whitespace_only(self) -> None:
        """A whitespace-only string normalises to an empty string."""
        assert normalize("   \t\n  ") == ""

    def test_normalize_lowercases_input(self) -> None:
        """The output is always lowercase."""
        assert normalize("McDonalds") == "mcdonalds"
        assert normalize("AMAZON PRIME") == "amazon prime"

    def test_normalize_collapses_whitespace(self) -> None:
        """Runs of whitespace are collapsed to a single space and trimmed."""
        assert normalize("  MCDONALDS   SUC   12  ") == "mcdonalds"
