"""A tiny synthetic FORCE datacube (EPSG:3035, 3 km tiles) for the tests."""

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

X_ORIGIN, Y_ORIGIN, TILE_SIZE = 4500000.0, 3300000.0, 3000.0

# QAI values: 0 = clear, 1 = no data, 2 = cloud (bits 1-2 = 01),
# 1 << 9 = saturation (removed by Aggressive only)
CLEAR, NODATA, CLOUD, SATURATED = 0, 1, 1 << 1, 1 << 9


def write(path, data, res, x0, y0, nodata=None):
    count, h, w = data.shape
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=count,
                       dtype="int16", crs=CRS.from_epsg(3035), nodata=nodata,
                       transform=from_origin(x0, y0, res, res)) as dst:
        dst.write(data.astype("int16"))


def scene(tile_dir, name, values, qai, res, x0, y0):
    n = int(TILE_SIZE / res)
    data = np.empty((len(values), n, n), dtype="int16")
    for i, v in enumerate(values):
        data[i] = v
    write(tile_dir / name, data, res, x0, y0, nodata=-9999)
    write(tile_dir / name.replace("BOA", "QAI"), np.full((1, n, n), qai), res, x0, y0)


@pytest.fixture(scope="session")
def datacube(tmp_path_factory):
    root = tmp_path_factory.mktemp("level2")
    wkt = CRS.from_epsg(3035).to_wkt()
    (root / "datacube-definition.prj").write_text(
        f"{wkt}\n-25.0\n60.0\n{X_ORIGIN}\n{Y_ORIGIN}\n{TILE_SIZE}\n{TILE_SIZE}\n")
    for tx, ty in [(0, 0), (1, 0)]:
        tile = root / f"X{tx:04d}_Y{ty:04d}"
        tile.mkdir()
        x0, y0 = X_ORIGIN + tx * TILE_SIZE, Y_ORIGIN - ty * TILE_SIZE
        lnd = [500, 800, 1000, 3000, 2000, 1000]   # BLUE GREEN RED NIR SWIR1 SWIR2
        sen = [500, 800, 1000, 600, 700, 800, 2900, 4000, 2000, 1000]
        scene(tile, "20240105_LEVEL2_LND08_BOA.tif", lnd, CLEAR, 30, x0, y0)
        scene(tile, "20240110_LEVEL2_LND09_BOA.tif", lnd, CLOUD, 30, x0, y0)
        scene(tile, "20240115_LEVEL2_SEN2A_BOA.tif", sen, CLEAR, 10, x0, y0)
        scene(tile, "20240120_LEVEL2_SEN2B_BOA.tif", sen, SATURATED, 10, x0, y0)
        scene(tile, "20240125_LEVEL2_SEN2B_BOA.tif", sen, NODATA, 10, x0, y0)
        scene(tile, "20230601_LEVEL2_SEN2A_BOA.tif", sen, CLEAR, 10, x0, y0)
    return root


@pytest.fixture()
def client(datacube):
    from forcelive import app as appmod
    appmod.app.config["LEVEL2_DIR"] = str(datacube)
    return appmod.app.test_client()


def lat_lng(x, y):
    from pyproj import Transformer
    lng, lat = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True).transform(x, y)
    return lat, lng
