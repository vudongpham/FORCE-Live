import argparse
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from flask import Flask, Response, jsonify, render_template, request
from flask import cli as flask_cli

from . import console
from .bands import ALL_BANDS, SEN2_ONLY, compute_value, has_band, needed_bands
from .utils import (
    CLOUD_MASK_LEVELS,
    OutsideImage,
    filter_scenes,
    get_cso_value,
    get_datacube,
    read_pixel,
)

SENSORS = {
    "LND04": "Landsat 4", "LND05": "Landsat 5", "LND07": "Landsat 7",
    "LND08": "Landsat 8", "LND09": "Landsat 9",
    "SEN2A": "Sentinel-2A", "SEN2B": "Sentinel-2B", "SEN2C": "Sentinel-2C",
}

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

# Pixel reads are I/O bound and GDAL releases the GIL, so a thread pool gives
# a near-linear speed-up (especially on network file systems).
DEFAULT_WORKERS = min(32, (os.cpu_count() or 4) * 4)
executor = None


def get_executor():
    global executor
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=app.config.get("WORKERS", DEFAULT_WORKERS),
            thread_name_prefix="forcelive",
        )
    return executor


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.status = "running"
        self.name = "Preparing data"
        self.current = 0    # acquisitions read
        self.total = 0      # acquisitions in the tile, date range and sensors
        self.images = 0     # acquisitions with data at the location
        self.valid = 0      # clear observations
        self.failed = 0     # files that could not be read
        self.last_error = None
        self.tile = None
        self.points = []    # [day, value, sensor, BOA file name] in completion order
        self.started = time.perf_counter()
        self.elapsed = 0.0
        self.futures = []

    def snapshot(self, since=0):
        with self.lock:
            return {
                "job": self.id,
                "status": self.status,
                "name": self.name,
                "current": self.current,
                "total": self.total,
                "images": self.images,
                "valid": self.valid,
                "failed": self.failed,
                "error": self.last_error,
                "elapsed": round(time.perf_counter() - self.started
                                 if self.status == "running" else self.elapsed, 3),
                "points": self.points[since:],
                "cursor": len(self.points),
            }

    def finish(self, status, name=None):
        with self.lock:
            if self.status != "running":
                return
            self.status = status
            if name is not None:
                self.name = name
            self.elapsed = time.perf_counter() - self.started
        report_finished(self)

    def cancel(self):
        self.cancelled.set()
        for f in self.futures:
            f.cancel()
        self.finish("cancelled", "Cancelled")


jobs = OrderedDict()
jobs_lock = threading.Lock()
MAX_JOBS = 32


def register_job(job):
    with jobs_lock:
        jobs[job.id] = job
        while len(jobs) > MAX_JOBS:
            _, old = jobs.popitem(last=False)
            old.cancel()


# ---------------------------------------------------------------------------
# Terminal output (one start and one result line per time series)
# ---------------------------------------------------------------------------

def short_error(text, limit=160):
    """Error message on one line, shortened."""
    one = " ".join(str(text).split()) or "unknown error"
    return one if len(one) <= limit else one[: limit - 1] + "…"


def sensor_summary(sensors):
    """e.g. "Landsat 5/7/8 + Sentinel-2A/B"."""
    lnd = [s[-1] if s[-2] == "0" else s[-2:] for s in sensors if s.startswith("LND")]
    sen = [s[-1] for s in sensors if s.startswith("SEN")]
    parts = []
    if lnd:
        parts.append("Landsat " + "/".join(lnd))
    if sen:
        parts.append("Sentinel-2" + "/".join(sen))
    return " + ".join(parts) or "no sensor"


def report_started(job, lat, lng, start, end, sensors, band, cloud_mask):
    mask = CLOUD_MASK_LEVELS.get(cloud_mask, cloud_mask)
    mask = "no cloud mask" if mask == "None" else f"{mask} mask"
    console.info("Time series",
                 f"{lat:.5f}, {lng:.5f} · {job.tile} · {start} → {end} · {band} · "
                 f"{mask} · {sensor_summary(sensors)} "
                 + console.style(f"({job.total} images)", "grey"))


def report_progress(job):
    with job.lock:
        current, total, valid = job.current, job.total, job.valid
    console.progress(job.id, current, total,
                     f"{current}/{total} images · {valid} clear",
                     force=current >= total)


def report_finished(job):
    console.end_progress(job.id)
    with job.lock:
        status, name, total, current = job.status, job.name, job.total, job.current
        images, valid, failed, err = job.images, job.valid, job.failed, job.last_error
        elapsed = console.fmt_seconds(job.elapsed)
    if status == "done" and not total:
        console.ok("Time series", name)
    elif status == "done":
        text = f"{valid} clear observations of {images} images · {elapsed}"
        if failed:
            console.warn("Time series", f"{text} · {failed} of {total} files could not be read "
                                        f"({short_error(err)})")
        else:
            console.ok("Time series", text)
    elif status == "error":
        console.error("Time series", short_error(name))
    elif status == "cancelled":
        console.stopped("Time series", f"stopped after {current}/{total} images · {elapsed}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def sample_scene(job, tile_path, scene, x, y, band, cso):
    """QAI screening + BOA read for one acquisition. Runs in the pool."""
    if job.cancelled.is_set():
        return
    _, day, sensor, boa_name = scene
    value, has_data, error = None, False, None
    try:
        qai = read_pixel(os.path.join(tile_path, boa_name.replace("BOA", "QAI")), x, y, (1,))[0]
        has_data = not qai & 1          # bit 0: no data
        if qai in cso:
            names, indexes = needed_bands(band, sensor)
            vals = read_pixel(os.path.join(tile_path, boa_name), x, y, indexes)
            value = compute_value(dict(zip(names, vals)), band)
    except OutsideImage:
        pass
    except Exception as exc:  # unreadable file: report it but keep going
        error = str(exc)
        console.warn("Read", f"{boa_name}: {short_error(exc)}")

    with job.lock:
        job.current += 1
        if has_data:
            job.images += 1
        if value is not None:
            job.valid += 1
            job.points.append([day, value, sensor, boa_name])
        if error:
            job.failed += 1
            job.last_error = error
        done = job.current >= job.total
    report_progress(job)
    if done:
        job.finish("done", "Done")


def start_job(job, l2dir, lat, lng, start_date, end_date, sensors, band, cloud_mask):
    try:
        cube = get_datacube(l2dir)
        tile, x, y, _, _ = cube.locate(lat, lng)
        job.tile = tile
        tile_path = os.path.join(l2dir, tile)
        if not os.path.isdir(tile_path):
            job.finish("error", f"Tile {tile} does not exist in the datacube")
            return

        # sensors without the band (e.g. red edge for Landsat) are left out
        sensors = [s for s in sensors if has_band(s, band)]
        scenes = filter_scenes(tile_path, start_date, end_date, sensors) if sensors else []
        cso = get_cso_value(cloud_mask)

        with job.lock:
            job.total = len(scenes)
            job.name = f"Reading {tile}"
        if not scenes:
            job.finish("done", "No images found")
            return
        report_started(job, lat, lng, start_date, end_date, sensors, band, cloud_mask)

        pool = get_executor()
        # newest first: the most interesting and the least likely to be cached
        job.futures = [pool.submit(sample_scene, job, tile_path, s, x, y, band, cso)
                       for s in reversed(scenes)]
    except Exception as exc:
        job.finish("error", f"Error: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", sensors=SENSORS)


@app.route("/sendData", methods=["POST"])
def send_data():
    data = request.get_json(force=True)
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("coordinates out of range")
        band = data.get("band", "NDVI")
        if band not in ALL_BANDS:
            raise ValueError(f"unknown band {band}")
        sensors = [s for s in (data.get("sensorList") or []) if s in SENSORS]
        if band in SEN2_ONLY and not any(s.startswith("SEN") for s in sensors):
            raise ValueError(f"{band} exists for Sentinel-2 only")
        cloud_mask = int(data.get("cloudMask", 1))
        if cloud_mask not in CLOUD_MASK_LEVELS:
            raise ValueError(f"unknown cloud mask {cloud_mask}")
        start_date = data["startDate"]
        end_date = data["endDate"]
        date.fromisoformat(start_date)
        date.fromisoformat(end_date)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "name": f"Bad request: {exc}"}), 400

    # cancel the job this client started before (new click supersedes it)
    prev = data.get("cancel")
    if prev:
        with jobs_lock:
            old = jobs.get(prev)
        if old:
            old.cancel()

    l2dir = app.config["LEVEL2_DIR"]
    tile_info = {}
    try:
        cube = get_datacube(l2dir)
        tile, _, _, tx, ty = cube.locate(lat, lng)
        tile_info = {"tile": tile, "outline": cube.tile_outline(tx, ty)}
    except Exception:
        pass

    job = Job()
    register_job(job)
    # scheduling is quick (directory index is cached), the reads run in the pool
    start_job(job, l2dir, lat, lng, start_date, end_date, sensors, band, cloud_mask)
    snap = job.snapshot()
    snap.update(tile_info)
    return jsonify(snap)


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "name": "Unknown job"}), 404
    since = request.args.get("since", default=0, type=int)
    return jsonify(job.snapshot(since))


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job:
        job.cancel()
    return jsonify({"status": "cancelled"})


@app.route("/tile")
def tile_lookup():
    """Tile name + outline for a coordinate (used for the hover/click overlay)."""
    try:
        cube = get_datacube(app.config["LEVEL2_DIR"])
        tile, _, _, tx, ty = cube.locate(request.args["lat"], request.args["lng"])
        exists = os.path.isdir(os.path.join(app.config["LEVEL2_DIR"], tile))
        return jsonify({"tile": tile, "exists": exists, "outline": cube.tile_outline(tx, ty)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


_overlay_error = None


def load_overlay():
    """The scene overlay module (needs numpy, imported on first use)."""
    global _overlay_error
    try:
        from . import overlay
        return overlay
    except ImportError as exc:
        if _overlay_error is None:
            console.warn("Overlay", f"scene overlay is not available: {exc}")
        _overlay_error = str(exc)
        return None


@app.route("/overlay/<int:z>/<int:x>/<int:y>.png")
def overlay_tile(z, x, y):
    """Web map tile of one acquisition (all datacube tiles of that date)."""
    ov = load_overlay()
    if ov is None:
        return jsonify({"error": f"Not available on this server: {_overlay_error}"}), 500
    l2dir = app.config["LEVEL2_DIR"]
    try:
        png = ov.cached_tile(l2dir, get_datacube(l2dir), request.args.get("scene", ""),
                             request.args.get("vis", "true"), z, x, y)
    except ov.OverlayError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        console.warn("Overlay", short_error(exc))
        return jsonify({"error": str(exc)}), 500
    return Response(png or ov.empty_png(), mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


def in_container():
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def browser_address(port, host_port=None):
    """Address shown in the banner. Inside a container the app cannot see
    which host port `docker run -p` maps to its port, so it has to be told
    (--host-port / HOST_PORT); otherwise the banner says so."""
    if host_port:
        return console.style(f"http://localhost:{host_port}", "bold")
    url = console.style(f"http://localhost:{port}", "bold")
    if in_container():
        return url + console.style(
            f"  (port inside the container: if you mapped another port with "
            f"docker -p PORT:{port}, open that one, or pass -e HOST_PORT=PORT to show it here)", "dim")
    return url


def main():
    parser = argparse.ArgumentParser(
        prog="forcelive",
        description="Web-based tool for inspecting FORCE datacube time-series data",
    )
    parser.add_argument(
        "level2Dir",
        help='FORCE datacube Level-2 directory path, the "datacube-definition.prj" file MUST exist in this directory',
    )
    parser.add_argument("-p", "--port", default=2741, type=int,
                        help="Port the app listens on (inside the container). Default: 2741")
    parser.add_argument("--host-port", type=int,
                        default=int(os.environ["HOST_PORT"]) if os.environ.get("HOST_PORT", "").isdigit() else None,
                        help="Port to open in the browser, if it differs from --port, e.g. the host "
                             "port of `docker run -p HOST_PORT:2741` (default: $HOST_PORT). "
                             "Only used for the address shown at start-up.")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Number of parallel reader threads. Default: {DEFAULT_WORKERS}")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode")
    parser.add_argument("--log-requests", action="store_true",
                        help="Also log every HTTP request (off by default to keep the output readable)")
    args = parser.parse_args()

    app.config["LEVEL2_DIR"] = args.level2Dir
    app.config["WORKERS"] = max(1, args.workers)

    # warm up: parse the datacube definition once at start-up
    try:
        cube = get_datacube(args.level2Dir)
    except Exception as exc:
        console.error("Datacube", short_error(exc, 300))
        console.line(" ", "", console.style(
            f'"{args.level2Dir}" must be the Level-2 folder that holds datacube-definition.prj', "dim"))
        raise SystemExit(1)
    try:
        n_tiles = sum(1 for e in os.scandir(args.level2Dir)
                      if e.is_dir() and e.name.startswith("X") and "_Y" in e.name)
    except OSError:
        n_tiles = 0

    crs = cube.crs.to_epsg()
    console.banner([
        ("Open in browser", browser_address(args.port, args.host_port)),
        ("Datacube", f"{args.level2Dir} · {n_tiles} tiles"),
        ("Grid", f"{'EPSG:' + str(crs) if crs else 'custom projection'} · "
                 f"tile size {cube.tile_size:g} m"),
        ("Parallel reads", str(app.config["WORKERS"])),
    ])
    if not console._tty:
        console.line(" ", "", console.style(
            "tip: run docker with -t to see a live progress bar", "dim"))

    if not (args.debug or args.log_requests):
        # no "GET /status/... 200" line for every poll of the browser, and no
        # Flask development-server banner (the banner above replaces it)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        flask_cli.show_server_banner = lambda *a, **k: None

    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True,
            use_reloader=args.debug)


if __name__ == "__main__":
    main()
