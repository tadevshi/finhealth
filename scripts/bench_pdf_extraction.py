"""Benchmark pdfplumber vs markitdown PDF extraction on a synthetic PDF.

Generates a multi-page bank-statement-shaped PDF (the same shape
CMF-mandated statements have: heading, cardholder paragraph, pipe-
delimited table) and times both extraction paths so we can quantify
the wall-time win that justifies the markitdown switch in the PR
description.

The benchmark is intentionally self-contained — it does **not**
depend on the real sample PDFs (which carry the cardholder's
personal data) and does **not** make any LLM call. The point is
to compare the *extraction* step, not the LLM call: the LLM time
savings reported in the PR description were measured separately
on the real PDFs, which require ``TEST_RUT`` to decrypt.

Run with::

    python scripts/bench_pdf_extraction.py
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

# Allow running the script from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdfplumber
from markitdown import MarkItDown
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


def build_synthetic_statement(
    out_path: Path, *, page_count: int = 8, rows_per_page: int = 40
) -> Path:
    """Build a multi-page bank-statement-shaped PDF on disk.

    Eight pages times forty rows mirrors the size of a real Santander
    statement closely enough that the conversion time is in the
    same order of magnitude; the LLM wall-time wins from
    markitdown scale with input size, so a representative shape
    is what we need to measure.
    """
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    story: list = []
    for page_idx in range(page_count):
        story.append(
            Paragraph("ESTADO DE CUENTA NACIONAL DE TARJETA DE CRÉDITO", styles["Heading1"])
        )
        story.append(Paragraph("NOMBRE DEL TITULAR LUIS SOTILLO AGUIAR", styles["Normal"]))
        story.append(Spacer(1, 12))
        data: list[list[str]] = [
            ["Fecha", "Descripción", "Monto $"],
            *[
                [
                    f"15/0{((i % 9) + 1)}/2025",
                    f"MERCHANT {i:03d}",
                    f"{(i + 1) * 1000:,}".replace(",", "."),
                ]
                for i in range(rows_per_page)
            ],
        ]
        table = Table(data)
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(table)
        if page_idx < page_count - 1:
            story.append(PageBreak())
    doc.build(story)
    return out_path


def extract_with_pdfplumber(pdf_path: Path) -> tuple[str, float]:
    """Extract text with the legacy :func:`pdfplumber.Page.extract_text`."""
    start = time.perf_counter()
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return text, time.perf_counter() - start


def extract_with_markitdown(pdf_path: Path) -> tuple[str, float]:
    """Extract structured Markdown with :mod:`markitdown`."""
    start = time.perf_counter()
    md = MarkItDown()
    result = md.convert(str(pdf_path))
    return result.text_content, time.perf_counter() - start


def main() -> None:
    runs = 3
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = Path(f.name)
    build_synthetic_statement(pdf_path, page_count=8, rows_per_page=40)
    size_kb = pdf_path.stat().st_size / 1024

    print(f"Synthetic PDF: {pdf_path} ({size_kb:.1f} KB, 8 pages x 40 rows)")
    print(f"Repeats: {runs}\n")

    pdfplumber_times: list[float] = []
    markitdown_times: list[float] = []
    pdfplumber_text = ""
    markitdown_text = ""

    for i in range(runs):
        pp_text, pp_t = extract_with_pdfplumber(pdf_path)
        md_text, md_t = extract_with_markitdown(pdf_path)
        pdfplumber_times.append(pp_t)
        markitdown_times.append(md_t)
        pdfplumber_text = pp_text
        markitdown_text = md_text
        print(f"  run {i + 1}: pdfplumber={pp_t * 1000:6.1f} ms   markitdown={md_t * 1000:6.1f} ms")

    print()
    print(
        f"pdfplumber: median={statistics.median(pdfplumber_times) * 1000:6.1f} ms   "
        f"min={min(pdfplumber_times) * 1000:6.1f} ms"
    )
    print(
        f"markitdown: median={statistics.median(markitdown_times) * 1000:6.1f} ms   "
        f"min={min(markitdown_times) * 1000:6.1f} ms"
    )

    speedup = statistics.median(pdfplumber_times) / statistics.median(markitdown_times)
    print(
        f"\nmarkitdown is {speedup:.2f}x {'faster' if speedup > 1 else 'slower'} than pdfplumber on this corpus."
    )

    print()
    print(
        f"Output sizes: pdfplumber={len(pdfplumber_text):,} chars   "
        f"markitdown={len(markitdown_text):,} chars"
    )
    print(f"  pdfplumber has pipe-delimited tables: {'|' in pdfplumber_text}")
    print(f"  markitdown has pipe-delimited tables: {'|' in markitdown_text}")
    print(f"  markitdown has Markdown separators: {'---' in markitdown_text}")

    pdf_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
