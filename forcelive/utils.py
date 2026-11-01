import os

# GDAL configuration -- must be set before rasterio/GDAL is initialised.
# FORCE tile directories often hold thousands of files (BOA, QAI, CLD, VZN, ...).
# By default GDAL lists the whole directory on every open to look for sidecar
# files (.aux.xml, .ovr, .msk); that listing dominates the cost of a
# single-pixel read, so we turn it off.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_PAM_ENABLED", "NO")
os.environ.setdefault("GDAL_CACHEMAX", "256")
os.environ.setdefault("VSI_CACHE", "FALSE")

import re  # noqa: E402
import threading  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime  # noqa: E402
from functools import lru_cache  # noqa: E402
from itertools import product  # noqa: E402

import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402

EPOCH = datetime(1970, 1, 1)

# FORCE Level-2 BOA band order
#   LND: BLUE, GREEN, RED, NIR, SWIR1, SWIR2
#   SEN2: BLUE, GREEN, RED, RE1, RE2, RE3, BNIR, NIR, SWIR1, SWIR2
BANDS_LND = {"BLUE": 1, "GREEN": 2, "RED": 3, "NIR": 4, "SWIR1": 5, "SWIR2": 6}
BANDS_SEN = {"BLUE": 1, "GREEN": 2, "RED": 3, "NIR": 8, "SWIR1": 9, "SWIR2": 10}


def band_indexes(band_name, sensor):
    """Return the 1-based band indexes that must be read for `band_name`."""
    table = BANDS_LND if sensor.startswith("LND") else BANDS_SEN
    if band_name == "NDVI":
        return (table["RED"], table["NIR"])
    return (table[band_name],)


# ---------------------------------------------------------------------------
# Datacube definition / tile lookup
# ---------------------------------------------------------------------------

def _extract_projection(f_string):
    if "=" in f_string:
        f_string = f_string.split("=", 1)[1]
    return f_string.lstrip().lstrip("*").strip()


def _extract_float(f_string):
    pattern = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?"
    return float(re.search(pattern, f_string).group())


class Datacube:
    """Parsed datacube-definition.prj with cached coordinate transformers."""

    def __init__(self, level_2_dir, prj_file_name="datacube-definition.prj"):
        prj_path = os.path.join(level_2_dir, prj_file_name)
        with open(prj_path, "r") as f:
            lines = [x.strip() for x in f.readlines()]

        self.crs = rasterio.CRS.from_wkt(_extract_projection(lines[0]))
        self.x_origin = _extract_float(lines[3])
        self.y_origin = _extract_float(lines[4])
        self.tile_size = _extract_float(lines[5])
        self.to_cube = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        self.to_wgs84 = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        self._lock = threading.Lock()  # pyproj transformers are not thread safe

    def locate(self, lat, lng):
        with self._lock:
            x, y = self.to_cube.transform(float(lng), float(lat))
        tile_x = int((x - self.x_origin) // self.tile_size)
        tile_y = int((self.y_origin - y) // self.tile_size)
        return f"X{tile_x:04d}_Y{tile_y:04d}", x, y, tile_x, tile_y

    def tile_outline(self, tile_x, tile_y, n=8):
        """Tile boundary as a list of [lat, lng] (densified, so it is correct
        on the web map even though the cube projection is not Mercator)."""
        x0 = self.x_origin + tile_x * self.tile_size
        y0 = self.y_origin - tile_y * self.tile_size
        x1, y1 = x0 + self.tile_size, y0 - self.tile_size
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        ring_x, ring_y = [], []
        for (ax, ay), (bx, by) in zip(corners[:-1], corners[1:]):
            for i in range(n):
                t = i / n
                ring_x.append(ax + t * (bx - ax))
                ring_y.append(ay + t * (by - ay))
        with self._lock:
            lng, lat = self.to_wgs84.transform(ring_x, ring_y)
        return [[float(a), float(b)] for a, b in zip(lat, lng)]


@lru_cache(maxsize=8)
def get_datacube(level_2_dir):
    return Datacube(level_2_dir)


def find_tile(lat, lng, level_2_dir, prj_file_name="datacube-definition.prj"):
    """Backwards compatible helper: returns (tile, x, y, lat, lng)."""
    tile, x, y, _, _ = get_datacube(level_2_dir).locate(lat, lng)
    return tile, x, y, lat, lng


# ---------------------------------------------------------------------------
# Quality screening
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def get_cso_value(best_quality=False):
    filtering_default = {
        'Valid data': ['0'],
        'Cloud state': ['00'],
        'Cloud shadow flag': ['0'],
        'Snow flag': ['0'],
        'Water flag': ['0', '1'],
        'Aerosol state': ['00', '01', '10', '11'],
        'Subzero flag': ['0'],
        'Saturation flag': ['0', '1'],
        'High sun zenith flag': ['0', '1'],
        'Illumination state': ['00', '01', '10', '11'],
        'Slope flag': ['0', '1'],
        'Water vapor flag': ['0', '1'],
        'Empty': ['0']
    }

    filtering_best = {
        'Valid data': ['0'],
        'Cloud state': ['00'],
        'Cloud shadow flag': ['0'],
        'Snow flag': ['0'],
        'Water flag': ['0', '1'],
        'Aerosol state': ['00'],
        'Subzero flag': ['0'],
        'Saturation flag': ['0'],
        'High sun zenith flag': ['0'],
        'Illumination state': ['00'],
        'Slope flag': ['0', '1'],
        'Water vapor flag': ['0', '1'],
        'Empty': ['0']
    }

    filtering_list = filtering_best if best_quality else filtering_default
    cso_value = [''.join(p) for p in product(*filtering_list.values())]
    return frozenset(int(x[::-1], 2) for x in cso_value)


# ---------------------------------------------------------------------------
# Tile directory index (cached, invalidated when the directory changes)
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()
_index_cache = {}


def _scan_tile(tile_dir):
    scenes = []
    with os.scandir(tile_dir) as it:
        for entry in it:
            name = entry.name
            if not name.endswith("BOA.tif"):
                continue
            try:
                day = (datetime.strptime(name[:8], "%Y%m%d") - EPOCH).days
                sensor = name.split("_")[2]
            except (ValueError, IndexError):
                continue
            scenes.append((name[:8], day, sensor, name))
    scenes.sort()
    return scenes


def list_tile_scenes(tile_dir):
    """Sorted list of (yyyymmdd, days_since_epoch, sensor, boa_name)."""
    mtime = os.stat(tile_dir).st_mtime_ns
    with _index_lock:
        hit = _index_cache.get(tile_dir)
        if hit and hit[0] == mtime:
            return hit[1]
    scenes = _scan_tile(tile_dir)
    with _index_lock:
        _index_cache[tile_dir] = (mtime, scenes)
    return scenes


def filter_scenes(tile_dir, start_date, end_date, sensors_list):
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    sensors = tuple(sensors_list)
    return [s for s in list_tile_scenes(tile_dir)
            if start <= s[0] <= end and s[2].startswith(sensors)]


def filter_images(tile_dir, start_date, end_date, sensors_list):
    """Backwards compatible helper: returns (boa_files, qai_files)."""
    boa = [s[3] for s in filter_scenes(tile_dir, start_date, end_date, sensors_list)]
    return boa, [b.replace("BOA", "QAI") for b in boa]


def days_since_epoch(date_str):
    return (datetime.strptime(date_str, "%Y%m%d") - EPOCH).days


# ---------------------------------------------------------------------------
# Pixel reading with an LRU cache
# ---------------------------------------------------------------------------

class PixelCache:
    """Thread-safe LRU of (path, row, col, band) -> value."""

    def __init__(self, maxsize=500_000):
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                self._data.move_to_end(key)
            return v

    def put(self, key, value):
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


pixel_cache = PixelCache()


class OutsideImage(Exception):
    pass


def read_pixel(path, x, y, bands):
    """Read the pixel covering map coordinate (x, y) for `bands` (1-based).

    Only a 1x1 window is requested, so GDAL decodes just the block(s) that
    hold that pixel. Results are cached so switching band / cloud mask /
    date range at the same location does not touch the disk again.
    """
    # grid geometry is identical for all images of a sensor family in a tile,
    # so we cache it per path to compute row/col without opening the file
    geo = _geo_cache.get(path)
    if geo is not None:
        row, col = _rowcol(geo, x, y)
        cached = [pixel_cache.get((path, row, col, b)) for b in bands]
        if all(v is not None for v in cached):
            return tuple(cached)

    with rasterio.open(path) as src:
        if geo is None:
            t = src.transform
            geo = (t.c, t.f, t.a, t.e, src.height, src.width)
            _geo_cache[path] = geo
            row, col = _rowcol(geo, x, y)
        if not (0 <= row < geo[4] and 0 <= col < geo[5]):
            raise OutsideImage(path)
        data = src.read(list(bands), window=((row, row + 1), (col, col + 1)))
    values = tuple(int(v) for v in data[:, 0, 0])
    for b, v in zip(bands, values):
        pixel_cache.put((path, row, col, b), v)
    return values


def _rowcol(geo, x, y):
    c, f, a, e = geo[:4]
    return int((y - f) // e), int((x - c) // a)


_geo_cache = {}
