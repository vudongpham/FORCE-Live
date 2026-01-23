from flask import Flask, render_template, request, jsonify
import threading
from .utils import find_tile, get_cso_value, filter_images, get_band_list, days_since_epoch
import rasterio
import os
import numpy as np
import time
import argparse

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static"
)

progress = {"name": 'Blank', "current": 0, "total": 0, "status": "idle"}
result_data = {"xdata": [], "ydata": []}

@app.route("/")
def index():
    return render_template("index.html")


def batch_sample_BOA_NDVI(image_paths, boa_files, x, y):
    red_list = []
    nir_list = []
    steps = len(image_paths)
    progress["name"] = 'Screening BOA'                    
    progress["total"] = steps
    progress["current"] = 0
    progress["status"] = "running"
    for i in range(len(image_paths)):
        sensor = boa_files[i].split('_')[2]
        if sensor[:3] == 'LND':
            red_band = 3
            nir_band = 4
        else:
            red_band = 3
            nir_band = 8
        
        with rasterio.open(image_paths[i]) as src:
            generator = src.sample([(x, y)], indexes=red_band)
            red_value = next(generator)[0]
            generator = src.sample([(x, y)], indexes=nir_band)
            nir_value = next(generator)[0]
            
        red_list.append(red_value)
        nir_list.append(nir_value)
        progress["current"] = i + 1 
    red_list = np.array(red_list)
    nir_list = np.array(nir_list)
    ndvi = ((nir_list - red_list) / (nir_list + red_list)) * 10000.
    ndvi = ndvi.astype(np.int16)
    return ndvi  


def batch_sample_BOA(image_paths, band_list, x, y):
    sampled_values = []
    steps = len(image_paths)
    progress["name"] = 'Screening BOA'                    
    progress["total"] = steps
    progress["current"] = 0
    progress["status"] = "running"

    for i in range(len(image_paths)):
        with rasterio.open(image_paths[i]) as src:
            sample_generator = src.sample([(x, y)], indexes=band_list[i])
            value = next(sample_generator)[0]  # Extract the first (and only) value
        sampled_values.append(value)
        progress["current"] = i + 1
    return np.array(sampled_values)


def batch_sample_QAI(image_paths, x, y):
    sampled_values = []
    steps = len(image_paths)
    progress["name"] = 'Screening QAI'                    
    progress["total"] = steps
    progress["current"] = 0
    progress["status"] = "running"
    for i in range(len(image_paths)):
        with rasterio.open(image_paths[i]) as src:
            sample_generator = src.sample([(x, y)], indexes=1)
            value = next(sample_generator)[0]  # Extract the first (and only) value
        sampled_values.append(value)
        progress["current"] = i + 1
    return np.array(sampled_values)

def run_job(l2dir, lat, lng, startDate, endDate, sensorList, band, cloudMaskOption):
    """
    Long-running job that fills progress while computing and finally
    stores the result in result_data.
    """
    global result_data, progress

    try:
        level_2_dir = l2dir
        tile, coord_x, coord_y, lat, lng = find_tile(lat, lng, level_2_dir=level_2_dir)
        tile_path = os.path.join(level_2_dir, tile)

        boa_files, qai_file = filter_images(
            tile_path,
            start_date=startDate,
            end_date=endDate,
            sensors_list=sensorList,
        )
        band_list = get_band_list(band, boa_files)

        date_list = [image[:8] for image in boa_files]
        date_list = np.array([days_since_epoch(d) for d in date_list])

        boa_files_path = [os.path.join(tile_path, image) for image in boa_files]
        qai_files_path = [os.path.join(tile_path, image) for image in qai_file]

        if band == 'NDVI':
            boa_values = batch_sample_BOA_NDVI(boa_files_path, boa_files, coord_x, coord_y)
        else:
            boa_values = batch_sample_BOA(boa_files_path, band_list, coord_x, coord_y)

        qai_values = batch_sample_QAI(qai_files_path, coord_x, coord_y)

        if cloudMaskOption == 1:
            cso_value = get_cso_value(best_quality=True)
        else:
            cso_value = get_cso_value(best_quality=False)

        mask = np.isin(qai_values, cso_value)

        y_value = boa_values[mask]
        x_value = date_list[mask]

        if len(x_value) > 0:
            x_value = x_value.tolist()
            y_value = y_value.tolist()
        else:
            x_value = []
            y_value = []

        # store results for the frontend
        result_data["xdata"] = x_value
        result_data["ydata"] = y_value

        # mark job as finished
        progress["status"] = "done"
    except Exception as exc:
        progress["name"] = f"Error: {exc}"
        progress["status"] = "error"


@app.route('/sendData', methods=['POST'])
def app_run():
    data = request.get_json()
    lat = data.get('lat')
    lng = data.get('lng')
    startDate = data.get('startDate')
    endDate = data.get('endDate')
    sensorList = data.get('sensorList')
    band = data.get('band')
    cloudMaskOption = int(data.get('cloudMask'))

    # read the startup arg from config
    l2dir = app.config["LEVEL2_DIR"]

    # reset progress + result at the start of a new request
    progress["name"] = "Preparing data"
    progress["current"] = 0
    progress["total"] = 0
    progress["status"] = "running"
    result_data["xdata"] = []
    result_data["ydata"] = []

    # run the heavy work in a background thread
    thread = threading.Thread(
        target=run_job,
        args=(l2dir, lat, lng, startDate, endDate, sensorList, band, cloudMaskOption),
        daemon=True,
    )
    thread.start()

    # return immediately; browser will poll /progress and /results
    return jsonify({"status": "started"})

@app.route("/results")
def get_results():
    return jsonify(result_data)

@app.route("/progress")
def get_progress():
    return jsonify(progress)

def main():
    parser = argparse.ArgumentParser(prog='FORCE Live', description="Web-based tool for inspecting force datacube time-series data", add_help=True)
    parser.add_argument(
        'level2Dir',
        help='FORCE datacube Level-2 directory path, the "datacube-definition.prj" file MUST exist in this directory'
    )
    parser.add_argument('-p', '--port',
                        help='Local port forwarding. Default: 2741',
                        default=2741)
    args = parser.parse_args()

    app.config["LEVEL2_DIR"] = args.level2Dir

    app.run(host="0.0.0.0", port=2741, debug=True)

if __name__ == "__main__":
    main()
