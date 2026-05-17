import pytest
from fastapi import HTTPException

from app.api.transactions import compute_exit_deductions, compute_netto


def test_compute_netto_positive():
    assert compute_netto(150.5, 50.25) == pytest.approx(100.25, 0.001)


def test_compute_netto_zero():
    assert compute_netto(100, 100) == 0.0


def test_compute_netto_negative_raises():
    with pytest.raises(HTTPException):
        compute_netto(50, 60)


def test_compute_exit_deductions_breakdown():
    result = compute_exit_deductions(
        1000,
        100,
        {
            "sampah_percent": 10,
            "air_percent": 5,
            "wajib_percent": 0,
            "t_panjang_percent": 2.5,
            "j_kosong_percent": 2.5,
        },
    )

    assert result["netto_1"] == 900.0
    assert result["total_percent"] == 20.0
    assert result["total_weight"] == pytest.approx(180.0, 0.001)
    assert result["netto_2"] == pytest.approx(720.0, 0.001)
    assert result["sampah_percent"] == pytest.approx(90.0, 0.001)
    assert result["air_percent"] == pytest.approx(45.0, 0.001)


def test_compute_exit_deductions_total_percent_limit():
    with pytest.raises(HTTPException):
        compute_exit_deductions(
            1000,
            100,
            {
                "sampah_percent": 50,
                "air_percent": 50,
                "wajib_percent": 1,
                "t_panjang_percent": 0,
                "j_kosong_percent": 0,
            },
        )
