import time

from conftest import TILE_SIZE, X_ORIGIN, Y_ORIGIN, lat_lng

from forcelive import app as appmod

LAT, LNG = lat_lng(X_ORIGIN + 1000, Y_ORIGIN - 1000)   # inside X0000_Y0000


def run(client, **kw):
    body = {"lat": LAT, "lng": LNG, "startDate": "2024-01-01", "endDate": "2024-12-31",
            "sensorList": ["LND08", "LND09", "SEN2A", "SEN2B"], "band": "NDVI", "cloudMask": 1}
    body.update(kw)
    r = client.post("/sendData", json=body)
    assert r.status_code == 200, r.get_json()
    first = r.get_json()
    points = list(first["points"])
    snap = first
    for _ in range(200):
        if snap["status"] != "running":
            break
        time.sleep(0.02)
        snap = client.get(f"/status/{first['job']}?since={len(points)}").get_json()
        points += snap["points"]
    return first, snap, points


def test_page(client):
    page = client.get("/").get_data(as_text=True)
    assert 'value="SEN2C"' in page and 'value="LND04"' in page and "Scene overlay" in page
    assert "downloadOverlay" not in page


def test_job_flow(client):
    first, snap, points = run(client)
    assert first["tile"] == "X0000_Y0000" and len(first["outline"]) > 4
    assert snap["status"] == "done"
    assert snap["total"] == 5 and snap["images"] == 4
    # Default: cloudy LND09 and no-data SEN2B removed, saturated SEN2B kept
    assert snap["valid"] == 3
    assert sorted((p[2], p[1]) for p in points) == [("LND08", 5000), ("SEN2A", 6000), ("SEN2B", 6000)]
    assert all(p[3].endswith("_BOA.tif") for p in points)


def test_cloud_mask_levels(client):
    assert run(client, cloudMask=0)[1]["valid"] == 4
    assert run(client, cloudMask=2)[1]["valid"] == 2


def test_sentinel_only_band(client):
    _, snap, points = run(client, band="RE1")
    assert {p[2] for p in points} == {"SEN2A", "SEN2B"} and {p[1] for p in points} == {600}
    r = client.post("/sendData", json={"lat": LAT, "lng": LNG, "startDate": "2024-01-01",
                                       "endDate": "2024-12-31", "sensorList": ["LND08"],
                                       "band": "RE1"})
    assert r.status_code == 400


def test_bad_request(client):
    r = client.post("/sendData", json={"lat": 100, "lng": 0, "startDate": "2020-01-01",
                                       "endDate": "2020-02-01"})
    assert r.status_code == 400
    r = client.post("/sendData", json={"lat": LAT, "lng": LNG, "startDate": "2020-01-01",
                                       "endDate": "2020-02-01", "band": "FOO"})
    assert r.status_code == 400


def test_tile_lookup(client):
    d = client.get(f"/tile?lat={LAT}&lng={LNG}").get_json()
    assert d["tile"] == "X0000_Y0000" and d["exists"]
    lat, lng = lat_lng(X_ORIGIN + 5 * TILE_SIZE, Y_ORIGIN - 1000)
    d = client.get(f"/tile?lat={lat}&lng={lng}").get_json()
    assert d["tile"] == "X0005_Y0000" and not d["exists"]


def test_overlay_tile(client):
    import math

    import numpy as np
    import warnings

    from rasterio.errors import NotGeoreferencedWarning
    from rasterio.io import MemoryFile

    warnings.simplefilter("ignore", NotGeoreferencedWarning)

    # web tile at zoom 14 holding the location
    z = 14
    n = 1 << z
    x = int((LNG + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(LAT))) / math.pi) / 2 * n)
    r = client.get(f"/overlay/{z}/{x}/{y}.png?scene=20240115_LEVEL2_SEN2A_BOA.tif&vis=true")
    assert r.status_code == 200 and r.mimetype == "image/png"
    with MemoryFile(r.data) as mem, mem.open() as src:
        rgba = src.read()
    assert rgba.shape == (4, 256, 256)
    opaque = rgba[3] == 255
    assert opaque.any()
    # RED 1000 of 0..3000, gamma 1.2
    expected = round((1000 / 3000) ** (1 / 1.2) * 255)
    assert abs(int(np.median(rgba[0][opaque])) - expected) <= 1

    # far away from the datacube: transparent
    r = client.get("/overlay/14/0/0.png?scene=20240115_LEVEL2_SEN2A_BOA.tif")
    assert r.status_code == 200
    with MemoryFile(r.data) as mem, mem.open() as src:
        assert not src.read(4).any()

    assert client.get(f"/overlay/{z}/{x}/{y}.png?scene=../x_BOA.tif").status_code == 400
    assert client.get(f"/overlay/{z}/{x}/{y}.png?scene=20240115_LEVEL2_SEN2A_BOA.tif&vis=foo").status_code == 400


def test_browser_address(monkeypatch):
    monkeypatch.setattr(appmod, "in_container", lambda: False)
    assert appmod.browser_address(2741) == "http://localhost:2741"
    assert appmod.browser_address(2741, 8027) == "http://localhost:8027"
    monkeypatch.setattr(appmod, "in_container", lambda: True)
    assert "HOST_PORT" in appmod.browser_address(2741)
