import pytest
from fastapi import HTTPException

from app.api.transactions import compute_netto


def test_compute_netto_positive():
    assert compute_netto(150.5, 50.25) == pytest.approx(100.25, 0.001)


def test_compute_netto_zero():
    assert compute_netto(100, 100) == 0.0


def test_compute_netto_negative_raises():
    with pytest.raises(HTTPException):
        compute_netto(50, 60)
