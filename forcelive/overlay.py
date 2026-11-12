"""Scene overlay: one acquisition of the datacube rendered as web map tiles.

Clicking an observation in the chart shows that scene on the map. The
browser asks for normal XYZ (Web Mercator) tiles; every tile is made from
the BOA file of that date in the datacube tile of the time series only
(not the neighbouring tiles), reprojected and stretched to an RGB PNG.
Rendered tiles are cached in memory.
"""

import math
import os
import re
import struct
import threading
import warnings
import zlib
from collections import OrderedDict

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning, RasterioIOError
from rasterio.crs import CRS
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window, WindowError
from rasterio.windows import from_bounds as window_from_bounds

from .bands import NODATA, band_table

# rasterio.warp.reproject() wraps a temporary in-memory dataset around the
# numpy arrays and silences this warning while it does so (the transform
# is set right after). That silencing uses warnings.catch_warnings(), which
# is not thread safe: when several map tiles are rendered at the same time,
# one thread can restore the warning filters while another is still inside,
# and a spurious "Dataset has no geotransform" warning is printed. The
# rendered tiles are correct, so the warning is turned off for rasterio.
warnings.filterwarnings("ignore", category=NotGeoreferencedWarning, module=r"rasterio(\.|$)")

TILE = 256
MERCATOR = CRS.from_epsg(3857)
HALF = 20037508.342789244
GAMMA = 1.2

# (bands, min, max) of reflectance x 10000, as in EE Time-Series Viewer
VIS_PRESETS = {
    "true":  (("RED", "GREEN", "BLUE"), 0, 3000),
    "false": (("NIR", "RED", "GREEN"), 0, 5000),
    "swir":  (("SWIR2", "NIR", "RED"), 0, 5000),
}

# FORCE Level-2 file name, e.g. 20240512_LEVEL2_SEN2A_BOA.tif (no folders)
SCENE_NAME = re.compile(r"^\d{8}_[A-Za-z0-9_.-]*BOA\.tif$")
# datacube tile folder, e.g. X0058_Y0045 or X0084_Y-013: FORCE zero-pads the
# index to 4 characters *including* the sign, so negatives have 3+ digits
TILE_NAME = re.compile(r"^X(?:\d{4,}|-\d{3,})_Y(?:\d{4,}|-\d{3,})$")


class OverlayError(ValueError):
    pass


def check_scene(name):
    if not SCENE_NAME.match(name or "") or "/" in name or "\\" in name or ".." in name:
        raise OverlayError("Invalid scene name")
    parts = name.split("_")
    if len(parts) < 3:
        raise OverlayError("Invalid scene name")
    return parts[2]   # sensor, e.g. SEN2A


def tile_bounds(z, x, y):
    """Web Mercator bounds (left, bottom, right, top) of XYZ tile z/x/y."""
    size = 2 * HALF / (1 << z)
    left = -HALF + x * size
    top = HALF - y * size
    return left, top - size, left + size, top


def check_tile(name):
    if not TILE_NAME.match(name or ""):
        raise OverlayError("Invalid tile name")
    return name


def _read_into(path, sensor, bands, dst, dst_transform):
    """Reproject `bands` of one BOA file into `dst` (3 x TILE x TILE, NaN
    outside the file)."""
    table = band_table(sensor)
    indexes = [table[b] for b in bands]
    with rasterio.open(path) as src:
        left, bottom, right, top = transform_bounds(
            MERCATOR, src.crs, *_dst_bounds(dst_transform), densify_pts=21)
        try:
            w = window_from_bounds(left, bottom, right, top, transform=src.transform)
            col0, row0 = math.floor(w.col_off), math.floor(w.row_off)
            col1 = math.ceil(w.col_off + w.width)
            row1 = math.ceil(w.row_off + w.height)
            win = Window(col0, row0, col1 - col0, row1 - row0).intersection(
                Window(0, 0, src.width, src.height))
        except WindowError:
            return
        if win.width <= 0 or win.height <= 0:
            return
        # decimated read: about the resolution of the web tile, not more
        out_w = max(1, min(int(win.width), TILE * 2))
        out_h = max(1, min(int(win.height), TILE * 2))
        data = src.read(indexes, window=win, out_shape=(len(indexes), out_h, out_w),
                        resampling=Resampling.nearest).astype(np.float32)
        wt = src.window_transform(win)
        t = Affine(wt.a * win.width / out_w, wt.b, wt.c, wt.d, wt.e * win.height / out_h, wt.f)
        src_crs = src.crs
    data[data == NODATA] = np.nan
    reproject(data, dst, src_transform=t, src_crs=src_crs, src_nodata=np.nan,
              dst_transform=dst_transform, dst_crs=MERCATOR, dst_nodata=np.nan,
              resampling=Resampling.nearest)


def _dst_bounds(t):
    return t.c, t.f + t.e * TILE, t.c + t.a * TILE, t.f


def encode_png(rgba):
    """PNG (8-bit RGBA) of a (4, h, w) uint8 array."""
    _, h, w = rgba.shape
    rows = np.ascontiguousarray(np.moveaxis(rgba, 0, -1)).reshape(h, w * 4)
    raw = np.hstack([np.zeros((h, 1), dtype=np.uint8), rows]).tobytes()  # filter 0 per row

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


EMPTY_PNG = None


def empty_png():
    global EMPTY_PNG
    if EMPTY_PNG is None:
        EMPTY_PNG = encode_png(np.zeros((4, TILE, TILE), dtype=np.uint8))
    return EMPTY_PNG


def render_tile(l2dir, tile, scene, vis, z, x, y):
    """PNG bytes of web tile z/x/y of `scene` (a BOA file name) in datacube
    tile `tile` (e.g. X0058_Y0045), or None if that file is not in the web
    tile."""
    sensor = check_scene(scene)
    check_tile(tile)
    if vis not in VIS_PRESETS:
        raise OverlayError(f"Unknown visualisation {vis!r}")
    bands, vmin, vmax = VIS_PRESETS[vis]
    table = band_table(sensor)
    if not all(b in table for b in bands):
        raise OverlayError(f"{vis} is not available for {sensor}")
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise OverlayError("Invalid tile")

    dst_transform = from_bounds(*tile_bounds(z, x, y), TILE, TILE)
    dst = np.full((3, TILE, TILE), np.nan, dtype=np.float32)
    try:
        _read_into(os.path.join(l2dir, tile, scene), sensor, bands, dst, dst_transform)
    except RasterioIOError:
        raise OverlayError(f"{scene} does not exist in {tile}")

    valid = ~np.isnan(dst).any(axis=0)
    if not valid.any():
        return None
    scaled = np.clip((np.nan_to_num(dst) - vmin) / (vmax - vmin), 0, 1) ** (1 / GAMMA)
    rgba = np.empty((4, TILE, TILE), dtype=np.uint8)
    rgba[:3] = np.round(scaled * 255)
    rgba[3] = np.where(valid, 255, 0)
    return encode_png(rgba)


class TileCache:
    def __init__(self, maxsize=3000):
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return True, self._data[key]
            return False, None

    def put(self, key, value):
        with self._lock:
            self._data[key] = value
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


tile_cache = TileCache()


def cached_tile(l2dir, tile, scene, vis, z, x, y):
    key = (l2dir, tile, scene, vis, z, x, y)
    hit, png = tile_cache.get(key)
    if not hit:
        png = render_tile(l2dir, tile, scene, vis, z, x, y)
        tile_cache.put(key, png)
    return png


# ---------------------------------------------------------------------------
# Cloud cover of a scene (shown in the information card on the map)
# ---------------------------------------------------------------------------

CLOUD_MAX_SIDE = 1000      # decimated read of the QAI file: at most this many pixels per side
cloud_cache = TileCache(maxsize=2000)


def cloud_cover(l2dir, tile, scene, level):
    """Share (%) of the valid pixels of `scene` in datacube tile `tile` that
    the cloud mask `level` removes (clouds, shadows, snow, ...), from the
    QAI file of the scene, read at a reduced resolution."""
    from .utils import get_cso_value

    check_scene(scene)
    check_tile(tile)
    key = (l2dir, tile, scene, level)
    hit, result = cloud_cache.get(key)
    if hit:
        return result
    clear_values = np.fromiter(get_cso_value(level), dtype=np.int64)
    path = os.path.join(l2dir, tile, scene.replace("BOA", "QAI"))
    try:
        with rasterio.open(path) as src:
            f = max(1, math.ceil(max(src.width, src.height) / CLOUD_MAX_SIDE))
            shape = (max(1, src.height // f), max(1, src.width // f))
            qai = src.read(1, out_shape=shape, resampling=Resampling.nearest).astype(np.int64)
            res = abs(src.transform.a) * src.width / shape[1]
    except RasterioIOError:
        raise OverlayError(f"{scene} has no QAI file in {tile}")
    valid = (qai & 1) == 0           # bit 0: no data
    n_valid = int(valid.sum())
    if n_valid:
        n_clear = int(np.isin(qai[valid], clear_values).sum())
        result = {"cloud": round(100 * (n_valid - n_clear) / n_valid, 2),
                  "valid_pixels": n_valid, "scale": round(res, 1)}
    else:
        result = {"cloud": None, "error": "no valid pixels"}
    cloud_cache.put(key, result)
    return result
