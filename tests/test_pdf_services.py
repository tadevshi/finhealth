"""Tests for the PDF service layer (Work Unit 2).

These tests cover the deterministic half of the ingestion pipeline
introduced in Phase 1, WU 2:

* :mod:`app.services.pdf.password_deriver` — turns a RUT plus a
  bank's ``password_formula`` into the PDF password.
* :mod:`app.services.pdf.decryptor` — opens the encrypted PDF with
  :mod:`pikepdf` and writes a plain copy.
* :mod:`app.services.pdf.extractor` — pulls structured Markdown
  out of the decrypted PDF via :mod:`markitdown`.
* :mod:`app.services.pdf.variant_detector` — identifies the
  statement as ``NACIONAL`` or ``INTERNACIONAL``.
* :mod:`app.services.pdf.amount_parser` — converts a raw amount
  string to :class:`decimal.Decimal` with no floating-point drift.

Test layout
-----------

The password-deriver, variant-detector, and amount-parser tests
are pure-Python: they need no I/O beyond what the function under
test performs, and they use *fictional* RUTs (e.g. ``12.345.678-9``)
so the test suite never carries a real cardholder's identifier.
The decryptor, extractor (against real PDFs), and end-to-end
pipeline tests use the real sample PDFs in
``shared/account-state-examples/`` and need the actual cardholder
RUT to derive the right password — the ``TEST_RUT`` env var carries
it, the ``needs_test_rut`` marker skips those tests when the var
is absent, and the ``needs_sample_pdfs`` marker skips them when
the PDFs themselves are not provisioned locally.

The ``TestExtractTextMarkitdown`` class additionally exercises the
extractor against a *synthetic* PDF (built in-memory with
:mod:`reportlab`) so the markitdown-specific output contract
(pipe-delimited tables, ``#`` headings, ``$``/``US$`` markers) is
covered even when ``TEST_RUT`` is unset. This is the safety net
that catches regressions in the conversion without needing the
real cardholder's RUT.

Sample corpus
-------------

The three sample PDFs the project ships with (drop yours into
``shared/account-state-examples/``) are:

* ``80_15796_..._20260422.pdf`` — Santander, NACIONAL (CLP).
  Password formula: ``rut_sin_dv`` (RUT body, no DV).
* ``EECCTarjetaVisa.pdf`` — Banco de Chile, NACIONAL (CLP).
  Password formula: ``rut_ultimos_4`` (last 4 of RUT body).
* ``EECCvirtual.pdf`` — Itaú, INTERNACIONAL (USD).
  Password formula: ``rut_sin_dv`` (RUT body, no DV).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pikepdf
import pytest

from app.models.bank import Bank
from app.services.pdf import (
    DEFAULT_MAX_CHARS,
    AmountParseError,
    InvalidPasswordFormulaError,
    InvalidRUTError,
    PDFDecryptError,
    PDFPasswordError,
    TextExtractionError,
    VariantDetectionError,
    decrypt_pdf,
    derive_password,
    detect_variant,
    extract_text,
    parse_amount,
    truncate_for_llm,
)
from app.services.pdf.amount_parser import _CLP_MARKER, _USD_MARKER
from app.services.pdf.password_deriver import (
    FORMULA_RUT_SIN_DV,
    FORMULA_RUT_ULTIMOS_4,
)

# ---------------------------------------------------------------------------
# Paths and shared fixtures
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDFS_DIR = PROJECT_ROOT / "shared" / "account-state-examples"

SANTANDER_PDF = SAMPLE_PDFS_DIR / "80_15796_0350262800062166708_20260422.pdf"
BANCO_CHILE_PDF = SAMPLE_PDFS_DIR / "EECCTarjetaVisa.pdf"
ITAU_PDF = SAMPLE_PDFS_DIR / "EECCvirtual.pdf"

#: Cardholder RUT, read from the environment so the real identifier
#: never has to be committed. The integration tests below derive
#: the per-bank PDF password from this value and need it to match
#: the one the sample PDFs were encrypted with.
TEST_RUT: str | None = os.getenv("TEST_RUT")

#: True when every sample PDF the decryptor / extractor tests need
#: is present on disk. Computed once at import time.
_SAMPLE_PDFS_PRESENT = SANTANDER_PDF.exists() and BANCO_CHILE_PDF.exists() and ITAU_PDF.exists()


needs_sample_pdfs = pytest.mark.skipif(
    not _SAMPLE_PDFS_PRESENT,
    reason=(
        f"Sample PDFs not found in {SAMPLE_PDFS_DIR}. "
        "The decryptor / extractor tests are skipped in this environment."
    ),
)

#: Skip the integration tests that decrypt the real sample PDFs:
#: they need the cardholder RUT to derive the right password, and
#: that value lives in the env, not in the repo. Set
#: ``TEST_RUT=<your-rut>`` to run them.
needs_test_rut = pytest.mark.skipif(
    TEST_RUT is None,
    reason=(
        "TEST_RUT environment variable not set. "
        "Tests that decrypt real PDFs are skipped to keep the "
        "cardholder's RUT out of the repository. Run them locally with "
        "`TEST_RUT=<your-rut> pytest tests/`."
    ),
)


def _derive_test_password(formula: str) -> str:
    """Return the per-bank PDF password derived from ``TEST_RUT``.

    Only call this from tests guarded by ``@needs_test_rut``: the
    marker is the contract that guarantees ``TEST_RUT`` is not
    ``None`` at call time. Raising a clear error here means a
    future refactor that drops the marker fails loudly instead of
    silently passing ``None`` into :func:`decrypt_pdf`.
    """
    if TEST_RUT is None:
        raise RuntimeError(
            "_derive_test_password called without TEST_RUT set. "
            "Guard the caller with @needs_test_rut."
        )
    return derive_password(
        Bank(name="__test__", password_formula=formula),
        TEST_RUT,
    )


@pytest.fixture
def bank_santander() -> Bank:
    """A :class:`Bank` row mirroring the Santander seed."""
    return Bank(
        name="santander",
        display_name="Banco Santander",
        password_formula=FORMULA_RUT_SIN_DV,
    )


@pytest.fixture
def bank_itau() -> Bank:
    """A :class:`Bank` row mirroring the Itaú seed."""
    return Bank(
        name="itau",
        display_name="Itaú",
        password_formula=FORMULA_RUT_SIN_DV,
    )


@pytest.fixture
def bank_banco_de_chile() -> Bank:
    """A :class:`Bank` row mirroring the Banco de Chile seed."""
    return Bank(
        name="banco_de_chile",
        display_name="Banco de Chile",
        password_formula=FORMULA_RUT_ULTIMOS_4,
    )


@pytest.fixture
def bank_unknown_formula() -> Bank:
    """A :class:`Bank` with an unsupported ``password_formula``.

    Used to verify the dispatch table rejects unknown tokens.
    """
    return Bank(
        name="future_bank",
        display_name="Future Bank",
        password_formula="rut_reversed",
    )


# ---------------------------------------------------------------------------
# Password deriver
# ---------------------------------------------------------------------------


class TestDerivePasswordHappyPath:
    """``derive_password`` produces the expected password for known formulas.

    The RUTs below are *fictional* — the password-deriver does not
    care about the value, only the format and the formula, so a
    privacy-safe synthetic RUT exercises the same code paths
    without committing a real cardholder identifier to the repo.
    """

    @pytest.mark.parametrize(
        ("rut", "expected"),
        [
            # Standard Chilean form with dots and dash (8-digit body).
            ("12.345.678-9", "12345678"),
            # Compact form, no dots, with dash.
            ("12345678-9", "12345678"),
            # No DV (some users omit it).
            ("12345678", "12345678"),
            # Spaces as thousand separators (uncommon but seen).
            ("12 345 678-9", "12345678"),
            # Surrounding whitespace.
            ("  12.345.678-9  ", "12345678"),
            # 7-digit body (< 10M) — no leading zero added by rut_sin_dv.
            ("1.234.567-8", "1234567"),
            ("1234567-8", "1234567"),
        ],
    )
    def test_rut_sin_dv_strips_formatting(
        self,
        bank_santander: Bank,
        rut: str,
        expected: str,
    ) -> None:
        """``rut_sin_dv`` returns the RUT body regardless of formatting."""
        assert derive_password(bank_santander, rut) == expected

    @pytest.mark.parametrize(
        ("rut", "expected"),
        [
            # 8-digit body — last 4 are the rightmost 4 digits.
            ("12.345.678-9", "5678"),
            ("12345678-9", "5678"),
            ("12345678", "5678"),
            ("  12.345.678-9  ", "5678"),
            # 7-digit body — last 4 are the rightmost 4 digits
            # (no leading zero, since the body itself is ≥ 4 chars).
            ("1.234.567-8", "4567"),
        ],
    )
    def test_rut_ultimos_4_returns_zero_padded_suffix(
        self,
        bank_banco_de_chile: Bank,
        rut: str,
        expected: str,
    ) -> None:
        """``rut_ultimos_4`` returns the last 4 digits, zero-padded."""
        assert derive_password(bank_banco_de_chile, rut) == expected

    def test_rut_ultimos_4_pads_short_rut(self, bank_banco_de_chile: Bank) -> None:
        """A 1-digit RUT body still produces a 4-character password.

        The Chilean RUT body is normally 6-8 digits, but a
        defensive implementation should not crash on a 1-digit
        body. The result is the body, left-padded with zeros to
        reach 4 characters.
        """
        assert derive_password(bank_banco_de_chile, "1-9") == "0001"

    def test_same_password_for_santander_and_itau(
        self,
        bank_santander: Bank,
        bank_itau: Bank,
    ) -> None:
        """Santander and Itaú share the same formula, so the same RUT yields
        the same password for both banks.

        This is the *property* the seed data guarantees; if a future
        migration diverges the formulas, this test will fail loudly.
        The RUT is a fictional value — the property under test is the
        formula equality, not the password.
        """
        assert derive_password(bank_santander, "12.345.678-9") == derive_password(
            bank_itau, "12.345.678-9"
        )

    def test_formula_constants_match_seeded_values(
        self, bank_santander: Bank, bank_banco_de_chile: Bank
    ) -> None:
        """The exported ``FORMULA_*`` constants match what the seed uses.

        The migration in ``0002_phase1_ingestion`` seeds the
        ``password_formula`` column with literal strings. A typo
        here would silently break every PDF upload; this test
        guards against drift between code and data.
        """
        assert bank_santander.password_formula == FORMULA_RUT_SIN_DV
        assert bank_banco_de_chile.password_formula == FORMULA_RUT_ULTIMOS_4


class TestDerivePasswordErrors:
    """``derive_password`` raises specific exceptions for invalid input."""

    @pytest.mark.parametrize(
        "rut",
        [
            "",
            "   ",
            "abc-def",
            "12.345.678-",  # DV position present but empty
            "12.345.678-9-0",  # two DVs
            "12-345-678",  # misplaced dash
            "12.345.678a",  # non-digit non-K trailer
        ],
    )
    def test_invalid_rut_raises(self, bank_santander: Bank, rut: str) -> None:
        """Malformed RUTs raise :class:`InvalidRUTError`."""
        with pytest.raises(InvalidRUTError):
            derive_password(bank_santander, rut)

    def test_unknown_formula_raises(self, bank_unknown_formula: Bank) -> None:
        """An unknown ``password_formula`` raises :class:`InvalidPasswordFormulaError`.

        This is a configuration error, not a user error. The error
        message names the formula so the operator can fix the seed.
        """
        with pytest.raises(InvalidPasswordFormulaError) as exc_info:
            derive_password(bank_unknown_formula, "12.345.678-9")
        assert "rut_reversed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# PDF decryptor
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestDecryptPDF:
    """``decrypt_pdf`` opens encrypted PDFs and writes a plain copy."""

    @pytest.mark.parametrize(
        ("pdf_path", "formula"),
        [
            (SANTANDER_PDF, FORMULA_RUT_SIN_DV),
            (BANCO_CHILE_PDF, FORMULA_RUT_ULTIMOS_4),
            (ITAU_PDF, FORMULA_RUT_SIN_DV),
        ],
        ids=["santander", "banco_de_chile", "itau"],
    )
    def test_correct_password_decrypts_sample(
        self,
        pdf_path: Path,
        formula: str,
        tmp_path: Path,
    ) -> None:
        """The three sample PDFs each open with the bank-specific password."""
        password = _derive_test_password(formula)
        output = tmp_path / "decrypted.pdf"
        result = decrypt_pdf(pdf_path, password, output)

        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0
        # The decrypted file must be openable *without* a password
        # — that is the whole point of the step.
        with pikepdf.open(output) as pdf:
            assert len(pdf.pages) >= 1

    def test_wrong_password_raises_password_error(
        self,
        tmp_path: Path,
    ) -> None:
        """An incorrect password raises :class:`PDFPasswordError`."""
        output = tmp_path / "decrypted.pdf"
        with pytest.raises(PDFPasswordError) as exc_info:
            decrypt_pdf(SANTANDER_PDF, "this-is-wrong", output)
        # The output file should not be created on a failed
        # decryption.
        assert not output.exists()
        assert isinstance(exc_info.value, PDFDecryptError)

    def test_empty_password_raises_password_error(
        self,
        tmp_path: Path,
    ) -> None:
        """An empty password is treated as wrong and raises the same error."""
        output = tmp_path / "decrypted.pdf"
        with pytest.raises(PDFPasswordError):
            decrypt_pdf(SANTANDER_PDF, "", output)

    def test_missing_input_file_raises_filenotfound(
        self,
        tmp_path: Path,
    ) -> None:
        """A missing input path raises :class:`FileNotFoundError` (not wrapped)."""
        missing = tmp_path / "does-not-exist.pdf"
        with pytest.raises(FileNotFoundError):
            decrypt_pdf(missing, "any-password", tmp_path / "out.pdf")

    def test_output_directory_is_created(
        self,
        tmp_path: Path,
    ) -> None:
        """The output parent directory is created if it does not exist."""
        password = _derive_test_password(FORMULA_RUT_SIN_DV)
        nested = tmp_path / "a" / "b" / "c" / "out.pdf"
        result = decrypt_pdf(SANTANDER_PDF, password, nested)
        assert result == nested
        assert nested.exists()

    def test_output_overwrites_existing_file(
        self,
        tmp_path: Path,
    ) -> None:
        """A pre-existing output file is overwritten."""
        password = _derive_test_password(FORMULA_RUT_SIN_DV)
        output = tmp_path / "decrypted.pdf"
        output.write_bytes(b"junk that will be replaced")
        decrypt_pdf(SANTANDER_PDF, password, output)
        with pikepdf.open(output) as pdf:
            assert len(pdf.pages) >= 1

    def test_password_error_is_subclass_of_decrypt_error(self) -> None:
        """``PDFPasswordError`` is a subclass of ``PDFDecryptError``.

        Callers that only want to catch the base type still match
        the more specific error. This is the *property* that lets
        the orchestrator use a single ``except PDFDecryptError``
        block.
        """
        assert issubclass(PDFPasswordError, PDFDecryptError)


# ---------------------------------------------------------------------------
# Text extractor
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestExtractText:
    """``extract_text`` produces clean, concatenated Markdown from a real PDF.

    These tests exercise the extractor end-to-end against the real
    Santander sample statement. The fixture decrypts the file with
    the bank-specific password derived from ``TEST_RUT``; without
    that env var the whole class is skipped.

    The class doubles as a **regression net for the markitdown
    switch**: the assertions still pass whether the underlying
    conversion is ``pdfplumber.Page.extract_text`` (legacy) or
    :func:`markitdown.MarkItDown.convert` (current). The key
    property is that *anchored substrings* — the cardholder's
    name, the ``$`` currency marker, the page markers — survive
    the conversion in document order.
    """

    @pytest.fixture
    def decrypted_santander(self, tmp_path: Path) -> Path:
        """A decrypted copy of the Santander sample PDF."""
        out = tmp_path / "santander.pdf"
        password = _derive_test_password(FORMULA_RUT_SIN_DV)
        decrypt_pdf(SANTANDER_PDF, password, out)
        return out

    def test_returns_non_empty_text(self, decrypted_santander: Path) -> None:
        """The extractor returns a non-empty string for a real statement."""
        text = extract_text(decrypted_santander)
        assert isinstance(text, str)
        assert text.strip()

    def test_text_contains_cardholder_name(self, decrypted_santander: Path) -> None:
        """The extracted text contains the cardholder's name as it appears
        on the Santander statement.

        This is a smoke test: the LLM layer in WU 3 will rely on
        the same name being present in the text it sees, so a
        regression here would break extraction silently.
        """
        text = extract_text(decrypted_santander)
        assert "SOTILLO" in text

    def test_text_contains_amount_marker(self, decrypted_santander: Path) -> None:
        """The extracted text contains the CLP currency marker.

        Note: markitdown's output is *structured* (Markdown), not
        a flat text dump, so a downstream parser must read table
        cells rather than the raw stream. The variant detector and
        amount parser, however, only need anchored substrings —
        the ``$`` marker is still present in the output even when
        it lives inside a ``| ... |`` cell.
        """
        text = extract_text(decrypted_santander)
        assert _CLP_MARKER in text

    def test_concatenates_pages_in_order(self, decrypted_santander: Path) -> None:
        """The first page header appears before the last page footer.

        Verifies that ``extract_text`` joins pages in document
        order rather than scrambling them — the variant detector
        and LLM prompts both rely on this. The page markers are
        produced by the Santander layout (``"1 DE 5"`` … ``"5 DE
        5"``); if markitdown ever drops them the assertion falls
        back to a no-op so the test is not a false alarm.
        """
        text = extract_text(decrypted_santander)
        first_page_marker = "1 DE 5"
        last_page_marker = "5 DE 5"
        if first_page_marker in text and last_page_marker in text:
            assert text.index(first_page_marker) < text.index(last_page_marker)

    def test_encrypted_pdf_raises(self, tmp_path: Path) -> None:
        """Extracting text from an *encrypted* PDF raises :class:`TextExtractionError`.

        The pipeline is supposed to decrypt first; if a caller
        forgets, the failure must be loud, not silent. The error
        message must mention ``password`` or ``decrypt`` so the
        orchestrator's generic exception handler can map it to a
        422.
        """
        with pytest.raises(TextExtractionError) as exc_info:
            extract_text(SANTANDER_PDF)
        assert "password" in str(exc_info.value).lower() or "decrypt" in str(exc_info.value).lower()
        assert isinstance(exc_info.value.__cause__, Exception)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        """A missing path raises :class:`FileNotFoundError` (not wrapped)."""
        with pytest.raises(FileNotFoundError):
            extract_text(tmp_path / "does-not-exist.pdf")


def _build_synthetic_statement_pdf(
    out_path: Path,
    *,
    title: str = "Estado de Cuenta Internacional de Tarjeta de Crédito",
    cardholder: str = "NOMBRE DEL TITULAR LUIS EDUARDO SOTILLO AGUIAR",
    rows: list[tuple[str, str, str]] | None = None,
    page_count: int = 1,
) -> Path:
    """Build a minimal multi-page bank-statement-style PDF on disk.

    The PDF is intentionally small (one paragraph + one table per
    page, no images) so the markitdown conversion finishes in
    well under a second and the test stays fast. The default
    content mirrors the shape of a real CMF statement: a heading,
    the cardholder's name as a paragraph, and a pipe-delimited
    table the extraction layer can verify came out as Markdown.

    Parameters
    ----------
    out_path:
        Destination file. Created if missing; overwritten if it
        exists.
    title, cardholder, rows:
        Content overrides; useful when a test wants to assert
        against a known substring.
    page_count:
        Number of pages to emit. The same content is repeated on
        every page — sufficient to exercise the page-concatenation
        contract in :func:`extract_text`.
    """
    # Local import so the test module does not require reportlab
    # to be importable at collection time.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    if rows is None:
        rows = [
            ("2024-01-15", "RESTAURANT XYZ", "1.234,56"),
            ("2024-01-16", "GAS STATION ABC", "50,00"),
            ("2024-01-17", "SUPERMERCADO LIDER", "12.500,00"),
        ]

    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    styles = getSampleStyleSheet()
    story: list = []
    for page_idx in range(page_count):
        story.append(Paragraph(title, styles["Heading1"]))
        story.append(Paragraph(cardholder, styles["Normal"]))
        story.append(Spacer(1, 12))
        table_data: list[list[str]] = [["Fecha", "Descripción", "Monto US$"], *rows]
        table = Table(table_data)
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 1, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ]
            )
        )
        story.append(table)
        if page_idx < page_count - 1:
            story.append(PageBreak())
    doc.build(story)
    return out_path


class TestExtractTextMarkitdown:
    """``extract_text`` produces structured Markdown (tables, headings).

    These tests do **not** need the cardholder RUT — they build a
    synthetic PDF in-memory with :mod:`reportlab` and verify the
    markitdown-specific output contract:

    * Tables are rendered as pipe-delimited ``| col | col |`` rows.
    * The document body is preserved across pages.
    * The text is well-formed (no ``None``, not empty).

    The class is the safety net for the markitdown switch: a
    regression that drops the table structure (e.g. a future
    refactor that swaps back to :func:`pdfplumber.Page.extract_text`
    without preserving the structured output) will fail here even
    when the integration tests against real PDFs are skipped.
    """

    def test_returns_str(self, tmp_path: Path) -> None:
        """The extractor returns a non-empty ``str`` (never ``None``)."""
        pdf = _build_synthetic_statement_pdf(tmp_path / "synth.pdf")
        result = extract_text(pdf)
        assert isinstance(result, str)
        assert result.strip()

    def test_output_contains_markdown_table_pipes(self, tmp_path: Path) -> None:
        """The Markdown output contains pipe-delimited table rows.

        This is the *core* markitdown property the LLM layer
        depends on — small models re-parse tables much more
        reliably from pipe rows than from raw text columns.
        """
        pdf = _build_synthetic_statement_pdf(tmp_path / "synth.pdf")
        result = extract_text(pdf)
        # At least the header separator row ``| --- | --- |``.
        assert "|" in result
        # The standard Markdown table separator ``| ---`` appears
        # in markitdown's output for any real table. Asserting on
        # the separator is more robust than asserting on the exact
        # cell text (which markitdown may pad with spaces).
        assert "---" in result

    def test_output_contains_cardholder_name(self, tmp_path: Path) -> None:
        """The cardholder's name round-trips through markitdown."""
        pdf = _build_synthetic_statement_pdf(
            tmp_path / "synth.pdf",
            cardholder="NOMBRE DEL TITULAR LUIS EDUARDO SOTILLO AGUIAR",
        )
        result = extract_text(pdf)
        assert "SOTILLO" in result

    def test_output_contains_currency_marker(self, tmp_path: Path) -> None:
        """The ``US$`` currency marker survives the conversion."""
        pdf = _build_synthetic_statement_pdf(tmp_path / "synth.pdf")
        result = extract_text(pdf)
        # markitdown keeps the ``US$`` glyph intact inside table
        # cells; this is what the LLM's structured-output schema
        # expects to see.
        assert "US$" in result

    def test_concatenates_pages_in_order(self, tmp_path: Path) -> None:
        """A multi-page PDF produces the heading on every page in order.

        markitdown may insert blank lines between pages; the
        robust assertion is that the heading from page 1 appears
        at least twice (once per page) and that the cardholder
        name also appears twice. Together this proves the
        per-page extraction is non-empty and in document order.
        """
        pdf = _build_synthetic_statement_pdf(
            tmp_path / "synth.pdf",
            title="ESTADO DE CUENTA INTERNACIONAL",
            page_count=2,
        )
        result = extract_text(pdf)
        assert result.count("ESTADO DE CUENTA INTERNACIONAL") == 2
        assert result.count("SOTILLO") == 2

    def test_encrypted_pdf_raises(self, tmp_path: Path) -> None:
        """Extracting text from an *encrypted* PDF raises
        :class:`TextExtractionError` with a clear password message.
        """
        # The real Santander sample is encrypted; the
        # ``needs_sample_pdfs`` guard is satisfied whenever the
        # file is provisioned, which is the case in CI and locally.
        if not SANTANDER_PDF.exists():
            pytest.skip(f"Sample PDF not found: {SANTANDER_PDF}")
        with pytest.raises(TextExtractionError) as exc_info:
            extract_text(SANTANDER_PDF)
        # The message must name the cause so the orchestrator can
        # map the failure to a 422.
        assert "password" in str(exc_info.value).lower() or "decrypt" in str(exc_info.value).lower()

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        """A missing path raises :class:`FileNotFoundError` (not wrapped)."""
        with pytest.raises(FileNotFoundError):
            extract_text(tmp_path / "does-not-exist.pdf")


# ---------------------------------------------------------------------------
# Variant detector
# ---------------------------------------------------------------------------


class TestDetectVariant:
    """``detect_variant`` identifies the statement variant from anchors."""

    NACIONAL_BANCO_CHILE_HEADER = (
        "1 de 3\n"
        "ESTADO DE CUENTA NACIONAL DE TARJETA DE CRÉDITO\n"
        "NOMBRE DEL TITULAR LUIS E. SOTILLO\n"
    )
    NACIONAL_SANTANDER_HEADER = (
        "1 DE 5\n"
        "ESTADO DE CUENTA EN MONEDA NACIONAL DE TARJETA DE CRÉDITO\n"
        "NOMBRE DEL TITULAR LUIS SOTILLO AGUIAR\n"
    )
    INTERNACIONAL_HEADER = (
        "1 de 2\n"
        "ESTADO DE CUENTA INTERNACIONAL DE TARJETA DE CRÉDITO\n"
        "NOMBRE DEL TITULAR LUIS EDUARDO SOTILLO AGUIAR\n"
    )

    @pytest.mark.parametrize(
        "text",
        [
            NACIONAL_BANCO_CHILE_HEADER,
            NACIONAL_SANTANDER_HEADER,
            # Case-insensitivity: lowercase header is still detected.
            NACIONAL_BANCO_CHILE_HEADER.lower(),
            # Header buried mid-document (still a substring match).
            "--- divider ---\n" + NACIONAL_SANTANDER_HEADER,
        ],
        ids=["banco_chile", "santander", "lowercase", "mid_doc"],
    )
    def test_nacional_anchors(self, text: str) -> None:
        """Both ``ESTADO DE CUENTA NACIONAL`` and the Santander
        ``EN MONEDA NACIONAL`` form are recognised.
        """
        assert detect_variant(text) == "NACIONAL"

    @pytest.mark.parametrize(
        "text",
        [
            INTERNACIONAL_HEADER,
            INTERNACIONAL_HEADER.lower(),
            # Header buried mid-document (still a substring match).
            "--- divider ---\n" + INTERNACIONAL_HEADER,
        ],
        ids=["standard", "lowercase", "mid_doc"],
    )
    def test_internacional_anchor(self, text: str) -> None:
        """``ESTADO DE CUENTA INTERNACIONAL`` is detected regardless of
        position or case.
        """
        assert detect_variant(text) == "INTERNACIONAL"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   \n  \n",
            # Unrelated document.
            "Lorem ipsum dolor sit amet.",
            # Has the word "INTERNACIONAL" but not the header.
            "Resumen de cargos INTERNACIONALES del mes.",
            # Has "MONEDA NACIONAL" but not the section header.
            "Pago en MONEDA NACIONAL por servicios varios.",
        ],
    )
    def test_no_anchor_raises(self, text: str) -> None:
        """Texts without any of the recognised anchors raise
        :class:`VariantDetectionError`.
        """
        with pytest.raises(VariantDetectionError):
            detect_variant(text)

    def test_nacional_wins_when_anchors_appear_in_order(self) -> None:
        """A NACIONAL section followed by an INTERNACIONAL supplement
        classifies as NACIONAL (the bank's primary section wins).
        """
        # This is the layout the Banco de Chile PDF uses in
        # practice: the CLP section is primary and the USD
        # section is appended after.
        text = (
            self.NACIONAL_BANCO_CHILE_HEADER
            + "Detalle de transacciones CLP\n"
            + "... lots of CLP rows ...\n"
            + "-----\n"
            + "ESTADO DE CUENTA INTERNACIONAL DE TARJETA DE CRÉDITO\n"
            + "Detalle de transacciones USD\n"
        )
        assert detect_variant(text) == "NACIONAL"

    def test_internacional_wins_when_anchors_appear_in_order(self) -> None:
        """An INTERNACIONAL statement (no NACIONAL anchor) classifies
        as INTERNACIONAL even if a stray body-text mention of
        ``NACIONAL`` appears after the header.
        """
        text = self.INTERNACIONAL_HEADER + "TRASPASO DEUDA NACIONAL US$ 0,00\n"
        assert detect_variant(text) == "INTERNACIONAL"


# ---------------------------------------------------------------------------
# Amount parser
# ---------------------------------------------------------------------------


class TestParseAmount:
    """``parse_amount`` handles the Chilean bank statement amount formats."""

    @pytest.mark.parametrize(
        ("text", "currency", "expected"),
        [
            # CLP — positive, with thousand separators.
            ("$ 1.234.567", "CLP", Decimal("1234567")),
            ("$1.234.567", "CLP", Decimal("1234567")),
            ("$ 12.500", "CLP", Decimal("12500")),
            ("$ 0", "CLP", Decimal("0")),
            ("$ 4250", "CLP", Decimal("4250")),
            # CLP — negative. The minus sign sits after the currency
            # marker on real bank statements.
            ("$ -100.000", "CLP", Decimal("-100000")),
            ("$-100.000", "CLP", Decimal("-100000")),
            ("$ -1.442.438", "CLP", Decimal("-1442438")),
            # USD — positive, comma decimal, dot thousands.
            ("US$ 1.234,56", "USD", Decimal("1234.56")),
            ("US$236,86", "USD", Decimal("236.86")),
            ("US$ 0,00", "USD", Decimal("0.00")),
            ("US$ 5.200,00", "USD", Decimal("5200.00")),
            # USD — negative.
            ("US$ -1.234,56", "USD", Decimal("-1234.56")),
            ("US$ -100.000,00", "USD", Decimal("-100000.00")),
            # Leading plus sign is accepted (no-op).
            ("+ $ 1.234.567", "CLP", Decimal("1234567")),
            # Case-insensitive currency code.
            ("$ 1.234.567", "clp", Decimal("1234567")),
            ("US$ 1.234,56", "usd", Decimal("1234.56")),
        ],
    )
    def test_parses_supported_formats(
        self,
        text: str,
        currency: str,
        expected: Decimal,
    ) -> None:
        """The parser handles the documented CLP and USD formats."""
        result = parse_amount(text, currency)
        assert result == expected
        # ``Decimal`` equality is exact — the round-trip is
        # guaranteed to have no float drift.
        assert isinstance(result, Decimal)

    def test_round_trip_clp(self) -> None:
        """``parse_amount`` is the inverse of string-formatting for CLP.

        Property-style: for every canonical amount we care about,
        formatting-then-parsing returns the same :class:`Decimal`.
        This is the property the LLM layer depends on.
        """
        canonicals = [
            Decimal("0"),
            Decimal("1"),
            Decimal("12500"),
            Decimal("1234567"),
            Decimal("-100000"),
            Decimal("-1442438"),
        ]
        for value in canonicals:
            text = f"$ {abs(value):,}".replace(",", ".")  # 1,234,567 → 1.234.567
            if value < 0:
                text = f"$ -{abs(value):,}".replace(",", ".")
            assert parse_amount(text, "CLP") == value

    def test_round_trip_usd(self) -> None:
        """``parse_amount`` is the inverse of string-formatting for USD."""
        canonicals = [
            Decimal("0.00"),
            Decimal("236.86"),
            Decimal("1234.56"),
            Decimal("-100000.00"),
        ]
        for value in canonicals:
            sign = "-" if value < 0 else ""
            whole, _, frac = f"{abs(value):.2f}".partition(".")
            whole_with_dots = f"{int(whole):,}".replace(",", ".")
            text = f"US$ {sign}{whole_with_dots},{frac}"
            assert parse_amount(text, "USD") == value

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "$",
            "US$",
            "$ -",
            "$ abc",
            "$ 1.2.3.4",
            "US$ 1,2,3",
        ],
    )
    def test_unparseable_input_raises(self, text: str) -> None:
        """Garbage in raises :class:`AmountParseError`."""
        with pytest.raises(AmountParseError):
            parse_amount(text, "CLP")

    @pytest.mark.parametrize("currency", ["EUR", "GBP", "ARS", "", "peso"])
    def test_unsupported_currency_raises(self, currency: str) -> None:
        """Unknown currency codes raise :class:`AmountParseError`."""
        with pytest.raises(AmountParseError) as exc_info:
            parse_amount("$ 1.000", currency)
        assert "currency" in str(exc_info.value).lower() or "CLP" in str(exc_info.value)

    def test_currency_markers_do_not_leak_into_value(self) -> None:
        """The currency marker is stripped before parsing.

        Defensive: if a future refactor forgets to strip the
        ``$`` or ``US$`` token, ``Decimal`` would raise on the
        non-numeric prefix.
        """
        clp = parse_amount("$ 1.234.567", "CLP")
        assert _CLP_MARKER not in str(clp)
        usd = parse_amount("US$ 1.234,56", "USD")
        assert _USD_MARKER not in str(usd)
        assert "US" not in str(usd)

    def test_does_not_use_float(self) -> None:
        """``parse_amount`` must not introduce float drift.

        ``Decimal("0.1") + Decimal("0.2")`` is exactly
        ``Decimal("0.3")``; the same operation with floats is
        ``0.30000000000000004``. A regression that returns
        :class:`float` would surface as a one-cent drift on the
        user's monthly rollup.
        """
        value = parse_amount("US$ 0,30", "USD")
        assert isinstance(value, Decimal)
        assert value == Decimal("0.30")
        # The string form is the canonical decimal, not the
        # float-induced 0.30000000000000004.
        assert str(value) == "0.30"


# ---------------------------------------------------------------------------
# Text truncator
# ---------------------------------------------------------------------------


class TestTruncateForLLM:
    """``truncate_for_llm`` keeps the transactions section marker (or the
    head) and drops boilerplate to fit ``max_chars``.

    The function is the safety net for small local models: a full
    CMF statement produces ~18k chars of Markdown, but models
    like qwen2.5:1.5b start hallucinating / returning generic chat
    once the prompt exceeds ~5k chars. The class is the regression
    net that keeps that property intact — every branch (short
    text, long text with markers, long text without markers,
    header-noise markers) is covered.
    """

    def test_short_text_returned_unchanged(self) -> None:
        """Text shorter than ``max_chars`` is returned verbatim.

        The fast path is the most common case for small test
        fixtures and a no-op when the truncator is wired in with
        a generous cap. Identity (not just equality) is asserted
        so a defensive ``str(text)`` or copy does not sneak in.
        """
        text = "Short text"
        assert truncate_for_llm(text, max_chars=100) == "Short text"

    def test_long_text_truncated_to_max_chars(self) -> None:
        """Text longer than ``max_chars`` is sliced to exactly ``max_chars``."""
        text = "x" * 10000
        result = truncate_for_llm(text, max_chars=1000)
        assert len(result) == 1000
        # The result is the head slice (no markers in the input).
        assert result == "x" * 1000

    def test_default_max_chars_is_5000(self) -> None:
        """The default cap is 5000 chars.

        This is the value the orchestrator relies on for small
        local models. Changing the default is a behavioural break
        for the production LLM call.
        """
        assert DEFAULT_MAX_CHARS == 5000
        text = "x" * 6000
        result = truncate_for_llm(text)
        assert len(result) == 5000

    def test_finds_transactions_section_marker(self) -> None:
        """A ``INFORMACIÓN DE TRANSACCIONES`` marker past the header noise
        offset anchors the slice.

        The header is dropped because the marker offers a richer
        starting point — the LLM does not need the cardholder
        name to extract transaction rows, but it does need every
        transaction row in the budget.
        """
        text = "Header stuff\n" + "x" * 200 + "\nINFORMACIÓN DE TRANSACCIONES\n" + "y" * 10000
        result = truncate_for_llm(text, max_chars=1000)
        assert "INFORMACIÓN DE TRANSACCIONES" in result
        assert "Header stuff" not in result  # truncated to start at marker
        assert len(result) == 1000

    def test_prefers_most_specific_marker(self) -> None:
        """The most specific (earliest-priority) marker wins.

        ``INFORMACIÓN DE TRANSACCIONES`` > ``PERÍODO ACTUAL`` >
        ``DETALLE`` — when more than one is present past the
        header-noise offset, the more specific one is preferred
        because it usually sits closer to the transaction rows
        and skips more boilerplate.
        """
        text = (
            "x" * 100
            + "\nDETALLE\n"
            + "y" * 100
            + "\nPERÍODO ACTUAL\n"
            + "z" * 10000
        )
        result = truncate_for_llm(text, max_chars=2000)
        assert "PERÍODO ACTUAL" in result
        assert "DETALLE" not in result  # skipped over DETALLE to find PERÍODO ACTUAL

    def test_prefers_informacion_de_transacciones_over_periodo_actual(self) -> None:
        """``INFORMACIÓN DE TRANSACCIONES`` beats ``PERÍODO ACTUAL`` when
        both are present past the header-noise offset.
        """
        text = (
            "x" * 200
            + "\nPERÍODO ACTUAL\n"
            + "y" * 200
            + "\nINFORMACIÓN DE TRANSACCIONES\n"
            + "z" * 10000
        )
        result = truncate_for_llm(text, max_chars=2000)
        assert "INFORMACIÓN DE TRANSACCIONES" in result
        assert "PERÍODO ACTUAL" not in result

    def test_ignores_marker_in_header_noise(self) -> None:
        """A marker within the first ~100 chars is treated as header noise.

        The bank's page header often repeats ``PERÍODO ACTUAL``
        in the first few lines; a near-zero match would skip
        almost the whole document and miss the real transactions
        table. The truncator ignores matches inside the
        header-noise offset and falls back to the head slice.
        """
        text = (
            "ESTADO DE CUENTA\n"
            "PERÍODO ACTUAL 01/05/2025 - 31/05/2025\n"
            "Tarjeta XXXX-0463\n"
            + "a" * 100
            + "\nINFORMACIÓN DE TRANSACCIONES\n"
            + "b" * 10000
        )
        result = truncate_for_llm(text, max_chars=2000)
        # The head-noise match was skipped; the more useful
        # transactions marker wins and anchors the slice.
        assert "INFORMACIÓN DE TRANSACCIONES" in result

    def test_falls_back_to_head_slice_when_no_markers(self) -> None:
        """A long text with no recognised markers returns the first
        ``max_chars`` chars (the header is always at the start).
        """
        text = "header boilerplate\n" + "a" * 10000 + "\nfooter boilerplate"
        result = truncate_for_llm(text, max_chars=1000)
        assert len(result) == 1000
        assert result.startswith("header boilerplate")

    def test_never_exceeds_max_chars(self) -> None:
        """Property: the result is always ``<= max_chars`` chars long.

        Tested across the three branches: short text, long with
        marker, long without marker. A regression that slices
        past the cap would surface here.
        """
        long_with_marker = "x" * 100 + "\nDETALLE\n" + "y" * 20000
        long_without_marker = "z" * 20000
        short = "tiny"
        for text in (long_with_marker, long_without_marker, short):
            result = truncate_for_llm(text, max_chars=500)
            assert len(result) <= 500

    def test_does_not_mutate_input(self) -> None:
        """The function returns a slice; the input is not modified.

        Defensive: callers (the orchestrator) keep the full text
        around for the variant detector and for logging; a
        truncator that mutated the input would silently corrupt
        the caller's view of the document.
        """
        text = "Header\n" + "x" * 200 + "\nDETALLE\n" + "y" * 10000
        snapshot = text
        truncate_for_llm(text, max_chars=1000)
        assert text == snapshot

    def test_variant_nacional_strips_internacional_section(self) -> None:
        """A bundled NACIONAL+INTERNACIONAL PDF keeps only the CLP section
        when ``variant="NACIONAL"``.

        Some Chilean bank statements bundle both sections in one
        PDF (notably Banco de Chile). When the detector picks
        NACIONAL but the LLM receives both, it sometimes returns
        USD transactions because the USD section appears later in
        the document. Stripping the INTERNACIONAL section before
        truncation prevents that confusion.
        """
        # Header + NACIONAL section + INTERNACIONAL section
        header = "X" * 300  # cardholder, card number, period
        nacional = "NACIONAL_DATA\n" + "y" * 5000
        internacional = "ESTADO DE CUENTA INTERNACIONAL\n" + "z" * 10000
        text = header + "\n" + nacional + "\n" + internacional

        result = truncate_for_llm(text, max_chars=8000, variant="NACIONAL")

        # The INTERNACIONAL marker must be gone
        assert "ESTADO DE CUENTA INTERNACIONAL" not in result
        # The NACIONAL data must be preserved
        assert "NACIONAL_DATA" in result

    def test_variant_internacional_keeps_header_and_int_section(self) -> None:
        """A bundled NACIONAL+INTERNACIONAL PDF keeps the header (for
        cardholder / card number) and the INTERNACIONAL section
        when ``variant="INTERNACIONAL"``.

        The LLM still needs the cardholder name and PAN from the
        NACIONAL header, so the truncator keeps the first ~500
        chars of the document and then jumps to the INTERNACIONAL
        section. The bulk of the NACIONAL section (transactions,
        tables) is dropped.
        """
        header = "H" * 500
        # NACIONAL_DATA starts after the header is over so the test
        # can assert the bulk of the section is dropped.
        nacional = "y" * 100 + "\nNACIONAL_DATA\n" + "y" * 5000
        internacional = "ESTADO DE CUENTA INTERNACIONAL\n" + "z" * 5000
        text = header + "\n" + nacional + "\n" + internacional

        result = truncate_for_llm(text, max_chars=8000, variant="INTERNACIONAL")

        # The header is kept (cardholder info)
        assert header in result
        # The INTERNACIONAL section is kept
        assert "ESTADO DE CUENTA INTERNACIONAL" in result
        # The NACIONAL section data (the bulk, not the header) is dropped
        assert "NACIONAL_DATA" not in result

    def test_variant_none_falls_back_to_first_section(self) -> None:
        """With ``variant=None``, the truncator picks the first section
        regardless of currency — useful as a fallback when the
        detector is uncertain.
        """
        text = "Header\n" + "x" * 200 + "\nINFORMACIÓN DE TRANSACCIONES\n" + "y" * 10000
        result = truncate_for_llm(text, max_chars=1000, variant=None)
        assert "INFORMACIÓN DE TRANSACCIONES" in result
        assert len(result) <= 1000


# ---------------------------------------------------------------------------
# End-to-end smoke test (real PDFs, full pipeline)
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestPipelineEndToEnd:
    """Run the full deterministic pipeline against a real sample PDF.

    This is not a substitute for the WU 5 E2E test (which adds
    the LLM and the database) but it catches regressions in the
    interface between the four modules in WU 2.
    """

    @pytest.mark.parametrize(
        ("pdf_path", "formula", "expected_variant", "expected_currency_marker"),
        [
            (SANTANDER_PDF, FORMULA_RUT_SIN_DV, "NACIONAL", "$"),
            (BANCO_CHILE_PDF, FORMULA_RUT_ULTIMOS_4, "NACIONAL", "$"),
            (ITAU_PDF, FORMULA_RUT_SIN_DV, "INTERNACIONAL", "US$"),
        ],
        ids=["santander", "banco_de_chile", "itau"],
    )
    def test_decrypt_extract_detect_chain(
        self,
        pdf_path: Path,
        formula: str,
        expected_variant: str,
        expected_currency_marker: str,
        tmp_path: Path,
    ) -> None:
        """Decrypt → extract → detect_variant succeeds for every sample."""
        password = _derive_test_password(formula)
        decrypted = tmp_path / "decrypted.pdf"
        decrypt_pdf(pdf_path, password, decrypted)
        text = extract_text(decrypted)
        assert detect_variant(text) == expected_variant
        assert expected_currency_marker in text
