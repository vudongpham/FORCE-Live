import pytest

from forcelive.bands import band_indexes, compute_value, has_band, needed_bands
from forcelive.utils import get_cso_value

LND = {"BLUE": 500, "GREEN": 800, "RED": 1000, "NIR": 3000, "SWIR1": 2000, "SWIR2": 1000}


def test_band_tables():
    assert band_indexes("NDVI", "LND08") == (4, 3)
    assert band_indexes("NDVI", "SEN2A") == (8, 3)
    assert needed_bands("RE1", "SEN2B") == (("RE1",), (4,))
    assert needed_bands("RE1", "LND08") is None
    assert has_band("SEN2C", "BNIR") and not has_band("LND05", "BNIR")
    assert has_band("LND05", "TCW") and not has_band("LND05", "FOO")
    with pytest.raises(ValueError):
        needed_bands("FOO", "LND08")


def test_indices():
    assert compute_value({"NIR": 3000, "RED": 1000}, "NDVI") == 5000
    assert compute_value({"NIR": 3000, "SWIR2": 1000}, "NBR") == 5000
    assert compute_value({"NIR": 3000, "SWIR1": 2000}, "MSI") == 6667
    assert compute_value({"NIR": 0, "RED": 0}, "NDVI") is None
    assert compute_value({"NIR": -9999, "RED": 1000}, "NDVI") is None
    assert compute_value({"RED": 1234}, "RED") == 1234
    tcg = compute_value(LND, "TCG")
    assert tcg == round(-0.1603 * 500 - 0.2819 * 800 - 0.4934 * 1000 + 0.7940 * 3000
                        - 0.0002 * 2000 - 0.1446 * 1000)
    evi = compute_value({"NIR": 3000, "RED": 1000, "BLUE": 500}, "EVI")
    assert evi == round(2.5 * 0.2 / (0.3 + 0.6 - 0.375 + 1) * 10000)


def test_cloud_mask_levels():
    none, default, best = get_cso_value(0), get_cso_value(1), get_cso_value(2)
    assert best < default < none
    assert 0 in best and 1 not in none          # bit 0: no data
    assert (1 << 1) in none and (1 << 1) not in default   # cloud
    assert (1 << 9) in default and (1 << 9) not in best   # saturation
    assert get_cso_value(True) == best          # old signature
