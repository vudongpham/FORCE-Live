"""Bands of the FORCE Level-2 BOA products and the indices computed from them.

Pure Python (no file access), so the index logic can be tested without data.
All reflectances are integers scaled by 10000 (FORCE BOA convention); the
indices are scaled by 10000 too.
"""

# FORCE Level-2 BOA band order (1-based)
#   LND:  BLUE, GREEN, RED, NIR, SWIR1, SWIR2
#   SEN2: BLUE, GREEN, RED, RE1, RE2, RE3, BNIR, NIR, SWIR1, SWIR2
BANDS_LND = {"BLUE": 1, "GREEN": 2, "RED": 3, "NIR": 4, "SWIR1": 5, "SWIR2": 6}
BANDS_SEN = {"BLUE": 1, "GREEN": 2, "RED": 3, "RE1": 4, "RE2": 5, "RE3": 6,
             "BNIR": 7, "NIR": 8, "SWIR1": 9, "SWIR2": 10}

REFLECTANCE_BANDS = ("BLUE", "GREEN", "RED", "RE1", "RE2", "RE3", "BNIR", "NIR", "SWIR1", "SWIR2")
SEN2_ONLY = ("RE1", "RE2", "RE3", "BNIR")
INDICES = ("NDVI", "EVI", "NBR", "NDMI", "NDWI", "MNDWI", "AWEI", "MSI", "TCB", "TCG", "TCW")
ALL_BANDS = REFLECTANCE_BANDS + INDICES

NODATA = -9999

# Tasseled Cap, reflectance-factor coefficients of Crist (1985) for
# BLUE, GREEN, RED, NIR, SWIR1, SWIR2 (as used by FORCE for Landsat and
# Sentinel-2). Linear, so they apply directly to reflectance x 10000.
TC_BANDS = ("BLUE", "GREEN", "RED", "NIR", "SWIR1", "SWIR2")
TC_COEFFS = {
    "TCB": (0.2043, 0.4158, 0.5524, 0.5741, 0.3124, 0.2303),
    "TCG": (-0.1603, -0.2819, -0.4934, 0.7940, -0.0002, -0.1446),
    "TCW": (0.0315, 0.2021, 0.3102, 0.1594, -0.6806, -0.6109),
}

# reflectance bands each index needs
NEEDS = {
    "NDVI": ("NIR", "RED"),
    "EVI": ("NIR", "RED", "BLUE"),
    "NBR": ("NIR", "SWIR2"),
    "NDMI": ("NIR", "SWIR1"),
    "NDWI": ("GREEN", "NIR"),
    "MNDWI": ("GREEN", "SWIR1"),
    "AWEI": ("GREEN", "NIR", "SWIR1", "SWIR2"),
    "MSI": ("NIR", "SWIR1"),
    "TCB": TC_BANDS, "TCG": TC_BANDS, "TCW": TC_BANDS,
}


def band_table(sensor):
    return BANDS_LND if sensor.startswith("LND") else BANDS_SEN


def has_band(sensor, band):
    """True if `band` (reflectance band or index) exists for `sensor`."""
    if band not in ALL_BANDS:
        return False
    table = band_table(sensor)
    return all(b in table for b in NEEDS.get(band, (band,)))


def needed_bands(band, sensor):
    """(canonical names, 1-based BOA band indexes) that must be read for
    `band` (a reflectance band or an index), or None if the sensor does not
    have them (e.g. red edge for Landsat)."""
    if band not in ALL_BANDS:
        raise ValueError(f"Unknown band {band!r}")
    if not has_band(sensor, band):
        return None
    names = NEEDS.get(band, (band,))
    table = band_table(sensor)
    return names, tuple(table[n] for n in names)


def band_indexes(band, sensor):
    """Backwards compatible helper: the 1-based band indexes to read."""
    nb = needed_bands(band, sensor)
    if nb is None:
        raise KeyError(band)
    return nb[1]


def _nd(a, b):
    if a + b == 0:
        return None
    return (a - b) / (a + b) * 10000.0


def compute_value(values, band):
    """Integer value (x 10000) of `band` from a dict of reflectances
    (x 10000, canonical band names). None if it cannot be computed
    (no data, zero denominator)."""
    if any(v is None or v == NODATA for v in values.values()):
        return None

    def r(b):
        return float(values[b])

    if band == "NDVI":
        v = _nd(r("NIR"), r("RED"))
    elif band == "NBR":
        v = _nd(r("NIR"), r("SWIR2"))
    elif band == "NDMI":
        v = _nd(r("NIR"), r("SWIR1"))
    elif band == "NDWI":   # McFeeters (1996)
        v = _nd(r("GREEN"), r("NIR"))
    elif band == "MNDWI":  # Xu (2006)
        v = _nd(r("GREEN"), r("SWIR1"))
    elif band == "AWEI":   # AWEI no-shadow, Feyisa et al. (2014); linear in reflectance
        v = 4.0 * (r("GREEN") - r("SWIR1")) - (0.25 * r("NIR") + 2.75 * r("SWIR2"))
    elif band == "MSI":    # Moisture Stress Index, Hunt & Rock (1989): SWIR1 / NIR
        v = None if r("NIR") <= 0 else r("SWIR1") / r("NIR") * 10000.0
    elif band == "EVI":
        n, red, blue = r("NIR") / 1e4, r("RED") / 1e4, r("BLUE") / 1e4
        den = n + 6.0 * red - 7.5 * blue + 1.0
        v = None
        if den != 0:
            v = 2.5 * (n - red) / den * 10000.0
            if not -20000 <= v <= 20000:  # unstable denominator
                v = None
    elif band in TC_COEFFS:
        v = sum(c * r(b) for c, b in zip(TC_COEFFS[band], TC_BANDS))
    elif band in REFLECTANCE_BANDS:
        v = r(band)
    else:
        raise ValueError(f"Unknown band {band!r}")
    return None if v is None else int(round(v))
