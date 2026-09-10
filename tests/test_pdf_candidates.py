from decimal import Decimal

import pytest

from app.services.pdf.candidates import mark_identity_collisions, parse_candidates

SANTANDER_NACIONAL_HEADER = "SANTANDER\nESTADO DE CUENTA NACIONAL DE TARJETA DE CRÉDITO\n"
SANTANDER_INTERNACIONAL_HEADER = "SANTANDER\nESTADO DE CUENTA INTERNACIONAL DE TARJETA DE CRÉDITO\n"


def test_candidate_identity_is_stable_for_same_document() -> None:
    text = SANTANDER_NACIONAL_HEADER + "15/05/2025 MERCADO $ 12.450\n16/05/2025 CAFE $ 2.000"
    first = parse_candidates(text=text, file_hash="a" * 64, variant="NACIONAL")
    second = parse_candidates(text=text, file_hash="a" * 64, variant="NACIONAL")

    assert [(c.row_identity, c.date, c.amount, c.currency) for c in first] == [
        (c.row_identity, c.date, c.amount, c.currency) for c in second
    ]
    assert all(len(candidate.row_identity) == 64 for candidate in first)


def test_candidate_collision_quarantine_marks_every_repeated_identity() -> None:
    candidate = parse_candidates(
        text=SANTANDER_NACIONAL_HEADER + "15/05/2025 MERCADO $ 12.450",
        file_hash="b" * 64,
        variant="NACIONAL",
    )[0]

    collided = mark_identity_collisions([candidate, candidate])

    assert len(collided) == 2
    assert {item.failure_code for item in collided} == {"identity_collision"}


def test_candidate_identity_includes_page_and_page_local_row_position() -> None:
    candidates = parse_candidates(
        text=SANTANDER_NACIONAL_HEADER + "15/05/2025 MERCADO $ 12.450\f15/05/2025 MERCADO $ 12.450",
        file_hash="c" * 64,
        variant="NACIONAL",
    )

    assert [(c.page_index, c.row_index) for c in candidates] == [(0, 2), (1, 0)]
    assert len({candidate.row_identity for candidate in candidates}) == 2


def test_candidate_parser_quarantines_malformed_transaction_like_evidence() -> None:
    candidates = parse_candidates(
        text=SANTANDER_NACIONAL_HEADER + "15/05/2025 MERCADO $ 12.450\n31/02/2025 BROKEN $ 4.000",
        file_hash="d" * 64,
        variant="NACIONAL",
    )

    malformed = candidates[1]
    assert len(candidates) == 2
    assert malformed.failure_code == "parse_error"
    assert malformed.date is None
    assert malformed.amount == Decimal("4000")
    assert len(malformed.row_identity) == 64


@pytest.mark.parametrize(
    ("variant", "fixture_text"),
    [
        (
            "NACIONAL",
            SANTANDER_NACIONAL_HEADER + "15/05/2026 SYNTHETIC MARKET $ 12.450",
        ),
        (
            "INTERNACIONAL",
            SANTANDER_INTERNACIONAL_HEADER + "15/05/2026 SYNTHETIC MARKET US$ 12,45",
        ),
    ],
)
def test_synthetic_layout_candidates_are_stable(variant: str, fixture_text: str) -> None:
    first = parse_candidates(text=fixture_text, file_hash="f" * 64, variant=variant)
    second = parse_candidates(text=fixture_text, file_hash="f" * 64, variant=variant)

    assert first
    assert [(c.row_identity, c.date, c.amount, c.currency) for c in first] == [
        (c.row_identity, c.date, c.amount, c.currency) for c in second
    ]
