import rasterio
from pyproj import Transformer
import os
import re
import numpy as np
from itertools import product
from datetime import datetime, timedelta

def find_tile(lat, lng, level_2_dir, prj_file_name="datacube-definition.prj"):

    def extract_projection(f_string):
        if '=' in f_string:
            f_string = f_string.split('=', 1)[1]
        return f_string.lstrip().lstrip('*').strip()

    def extract_float(f_string):
        pattern = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)"
        value = float(re.search(pattern, f_string).group())
        return value
    
    prj_dir = os.path.join(level_2_dir, prj_file_name)

    with open(prj_dir, "r") as file:
        prj_lines = file.readlines()  

    prj_lines = [x.strip() for x in prj_lines]

    proj_wkt = extract_projection(prj_lines[0])

    target_crs = rasterio.CRS.from_wkt(proj_wkt)

    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)

    x_origin = float(extract_float(prj_lines[3]))
    y_origin = float(extract_float(prj_lines[4]))
    tile_size = float(extract_float(prj_lines[5]))

    x_test, y_test = transformer.transform(lng, lat)

    tile_X = int(np.floor((x_test - x_origin) / tile_size))
    tile_Y = int(np.floor((y_origin - y_test) / tile_size))

    tile_found = f"X{tile_X:04d}_Y{tile_Y:04d}"

    return tile_found, x_test, y_test, lat, lng

def get_cso_value(best_quality=False):
    filtering_default = {
        'Valid data' : ['0'],
        'Cloud state' : ['00'],
        'Cloud shadow flag' : ['0'],
        'Snow flag' : ['0'],
        'Water flag': ['0', '1'],
        'Aerosol state' : ['00', '01', '10', '11'],
        'Subzero flag' : ['0'],
        'Saturation flag' : ['0', '1'],
        'High sun zenith flag' : ['0', '1'],
        'Illumination state' : ['00', '01', '10', '11'],
        'Slope flag' : ['0', '1'],
        'Water vapor flag' : ['0', '1'],
        'Empty' : ['0']
    }

    filtering_best = {
        'Valid data' : ['0'],
        'Cloud state' : ['00'],
        'Cloud shadow flag' : ['0'],
        'Snow flag' : ['0'],
        'Water flag': ['0', '1'],
        'Aerosol state' : ['00'],
        'Subzero flag' : ['0'],
        'Saturation flag' : ['0'],
        'High sun zenith flag' : ['0'],
        'Illumination state' : ['00'],
        'Slope flag' : ['0', '1'],
        'Water vapor flag' : ['0', '1'],
        'Empty' : ['0']
    }

    if best_quality:
        filtering_list = filtering_best
    else:
        filtering_list = filtering_default
    cso_value = [''.join(p) for p in product(*filtering_list.values())]
    cso_value = [x[::-1] for x in cso_value]
    cso_value = [int(x, 2) for x in cso_value]
    cso_value.sort()
    return cso_value


def filter_images(tile_dir, start_date, end_date, sensors_list):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    boa_files = []

    for filename in os.listdir(tile_dir):
        if filename.endswith("BOA.tif"):
            try:
                file_date_str = filename[:8]  # Extract YYYYMMDD
                file_date = datetime.strptime(file_date_str, "%Y%m%d")
                
                if start_dt <= file_date <= end_dt:
                    boa_files.append(filename)
            except ValueError:
                continue  # Skip files that don't match the expected pattern
    
    boa_files = [image for image in boa_files if any(sensor in image for sensor in sensors_list)]
    boa_files.sort()
    qai_files = [image.replace('BOA', 'QAI') for image in boa_files]

    return boa_files, qai_files


def get_band_list(band_name, boa_files):

    allowed_bands_LND = {
        'NDVI': 0,
        'RED' : 1,
        'GREEN' : 2, 
        'BLUE': 3,
        'NIR': 4,
        'SWIR1': 5,
        'SWIR2': 6
    }

    allowed_bands_SEN = {
        'NDVI': 0,
        'RED' : 1,
        'GREEN' : 2, 
        'BLUE': 3,
        'NIR': 8,
        'SWIR1': 9,
        'SWIR2': 10
    }

    sensor_list = [image.split('_')[2] for image in boa_files]

    band_list = []

    for sensor in sensor_list:
        if sensor[:3] == 'LND':
            band_list.append(allowed_bands_LND[band_name])
        else:
            band_list.append(allowed_bands_SEN[band_name])
    return band_list

def days_since_epoch(date_str):
    # Convert string to datetime object
    date_obj = datetime.strptime(date_str, "%Y%m%d")
    
    # Define the Unix epoch start date
    epoch = datetime(1970, 1, 1)
    
    # Compute the difference in days
    return (date_obj - epoch).days