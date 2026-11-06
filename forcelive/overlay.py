"""Scene overlay: one acquisition of the datacube rendered as web map tiles.

Clicking an observation in the chart shows that scene on the map. The
browser asks for normal XYZ (Web Mercator) tiles; every tile is put
together from the BOA files of that date in all datacube tiles it touches
(the same file name exists in each tile folder), reprojected and stretched
to an RGB PNG. Rendered tiles are cached in memory.
"""

import math
import os
import re
import struct
import threading
import zlib
from collections import OrderedDict

import numpy as np
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.crs import CRS
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window, WindowError
from rasterio.windows import from_bounds as window_from_bounds

from .bands import NODATA, band_table

TILE = 256
MERCATOR = CRS.from_epsg(3857)
HALF = 20037508.342789244
MAX_CUBE_TILES = 16      # a web tile touching more datacube tiles is not rendered (zoom in)
GAMMA = 1.2

# (bands, min, max) of reflectance x 10000, as in EE Time-Series Viewer
VIS_PRESETS = {
    "true":  (("RED", "GREEN", "BLUE"), 0, 3000),
    "false": (("NIR", "RED", "GREEN"), 0, 5000),
    "swir":  (("SWIR2", "NIR", "RED"), 0, 5000),
}

# FORCE Level-2 file name, e.g. 20240512_LEVEL2_SEN2A_BOA.tif (no folders)
SCENE_NAME = re.compile(r"^\d{8}_[A-Za-z0-9_.-]*BOA\.tif$")


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


def cube_tiles(cube, bounds):
    """Datacube tile indexes (tx, ty) touched by Web Mercator `bounds`."""
    try:
        minx, miny, maxx, maxy = transform_bounds(MERCATOR, cube.crs, *bounds, densify_pts=21)
    except Exception:
        return []
    if not all(map(math.isfinite, (minx, miny, maxx, maxy))):
        return []
    ts = cube.tile_size
    tx0 = int((minx - cube.x_origin) // ts)
    tx1 = int((maxx - cube.x_origin) // ts)
    ty0 = int((cube.y_origin - maxy) // ts)
    ty1 = int((cube.y_origin - miny) // ts)
    if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > MAX_CUBE_TILES:
        return None
    return [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def _read_into(path, sensor, bands, dst, dst_transform):
    """Reproject `bands` of one BOA file into `dst` (3 x TILE x TILE, NaN
    where still empty); only fills pixels that are empty."""
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
    tmp = np.full_like(dst, np.nan)
    reproject(data, tmp, src_transform=t, src_crs=src_crs, src_nodata=np.nan,
              dst_transform=dst_transform, dst_crs=MERCATOR, dst_nodata=np.nan,
              resampling=Resampling.nearest)
    empty = np.isnan(dst)
    dst[empty] = tmp[empty]


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


def render_tile(l2dir, cube, scene, vis, z, x, y):
    """PNG bytes of web tile z/x/y of `scene` (a BOA file name), or None
    if nothing of the scene is in the tile."""
    sensor = check_scene(scene)
    if vis not in VIS_PRESETS:
        raise OverlayError(f"Unknown visualisation {vis!r}")
    bands, vmin, vmax = VIS_PRESETS[vis]
    table = band_table(sensor)
    if not all(b in table for b in bands):
        raise OverlayError(f"{vis} is not available for {sensor}")
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise OverlayError("Invalid tile")

    bounds = tile_bounds(z, x, y)
    tiles = cube_tiles(cube, bounds)
    if not tiles:
        return None
    dst_transform = from_bounds(*bounds, TILE, TILE)
    dst = np.full((3, TILE, TILE), np.nan, dtype=np.float32)
    found = False
    for tx, ty in tiles:
        path = os.path.join(l2dir, f"X{tx:04d}_Y{ty:04d}", scene)
        try:
            _read_into(path, sensor, bands, dst, dst_transform)
            found = True
        except RasterioIOError:
            continue    # no such file: the scene does not cover this tile
    if not found:
        return None

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


def cached_tile(l2dir, cube, scene, vis, z, x, y):
    key = (l2dir, scene, vis, z, x, y)
    hit, png = tile_cache.get(key)
    if not hit:
        png = render_tile(l2dir, cube, scene, vis, z, x, y)
        tile_cache.put(key, png)
    return png
