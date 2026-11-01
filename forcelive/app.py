import argparse
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request

from .utils import (
    OutsideImage,
    band_indexes,
    filter_scenes,
    get_cso_value,
    get_datacube,
    read_pixel,
)

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
        self.current = 0
        self.total = 0
        self.valid = 0
        self.points = []  # [day, value, sensor] in completion order
        self.error = None
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
                "valid": self.valid,
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


def sample_scene(job, tile_path, scene, x, y, band, cso):
    """QAI screening + BOA read for one acquisition. Runs in the pool."""
    if job.cancelled.is_set():
        return
    _, day, sensor, boa_name = scene
    value = None
    try:
        qai = read_pixel(os.path.join(tile_path, boa_name.replace("BOA", "QAI")), x, y, (1,))[0]
        if qai in cso:
            bands = band_indexes(band, sensor)
            vals = read_pixel(os.path.join(tile_path, boa_name), x, y, bands)
            if band == "NDVI":
                red, nir = vals
                if nir + red != 0:
                    value = int(round((nir - red) / (nir + red) * 10000.0))
            else:
                value = vals[0]
    except OutsideImage:
        pass
    except Exception as exc:  # unreadable file: report it but keep going
        app.logger.warning("Failed to read %s: %s", boa_name, exc)

    with job.lock:
        job.current += 1
        if value is not None:
            job.valid += 1
            job.points.append([day, value, sensor])
        done = job.current >= job.total
    if done:
        job.finish("done", "Done")


def start_job(job, l2dir, lat, lng, start_date, end_date, sensors, band, cloud_mask):
    try:
        cube = get_datacube(l2dir)
        tile, x, y, _, _ = cube.locate(lat, lng)
        tile_path = os.path.join(l2dir, tile)
        if not os.path.isdir(tile_path):
            job.finish("error", f"Tile {tile} does not exist in the datacube")
            return

        scenes = filter_scenes(tile_path, start_date, end_date, sensors)
        cso = get_cso_value(best_quality=(cloud_mask == 1))

        with job.lock:
            job.total = len(scenes)
            job.name = f"Reading {tile}"
        if not scenes:
            job.finish("done", "No images found")
            return

        pool = get_executor()
        job.futures = [pool.submit(sample_scene, job, tile_path, s, x, y, band, cso)
                       for s in scenes]
    except Exception as exc:
        job.finish("error", f"Error: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sendData", methods=["POST"])
def send_data():
    data = request.get_json(force=True)
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
        band = data.get("band", "NDVI")
        band_indexes(band, "LND")  # validate
        sensors = list(data.get("sensorList") or [])
        cloud_mask = int(data.get("cloudMask", 0))
        start_date = data["startDate"]
        end_date = data["endDate"]
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


def main():
    parser = argparse.ArgumentParser(
        prog="FORCE Live",
        description="Web-based tool for inspecting force datacube time-series data",
        add_help=True,
    )
    parser.add_argument(
        "level2Dir",
        help='FORCE datacube Level-2 directory path, the "datacube-definition.prj" file MUST exist in this directory',
    )
    parser.add_argument("-p", "--port", help="Local port forwarding. Default: 2741", default=2741, type=int)
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Number of parallel reader threads. Default: {DEFAULT_WORKERS}")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode")
    args = parser.parse_args()

    app.config["LEVEL2_DIR"] = args.level2Dir
    app.config["WORKERS"] = max(1, args.workers)

    # warm up: parse the datacube definition once at start-up
    get_datacube(args.level2Dir)

    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True,
            use_reloader=args.debug)


if __name__ == "__main__":
    main()
