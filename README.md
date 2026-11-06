# FORCE Live
Web-based visualizing time-series data from FORCE datacube.
Data extraction and cloud masking are performed on-the-fly.

<img width="1881" height="907" alt="force" src="https://github.com/user-attachments/assets/cce099d7-8203-4dc1-87e4-358a9989f6cf" />

### 1. Installation
Pull the docker image
```
docker pull vudongpham/forcelive:latest
```


### 2. Run

```
docker run --rm -t \
  -v /your/datacube/level2/dir:/l2dir \
  -p 2741:2741 \
  vudongpham/forcelive forcelive /l2dir
```
Replace the `/your/datacube/level2/dir` with your dir!

To use another port, change only the host side of `-p` and pass it as `HOST_PORT`, so the start-up message shows the right address:
```
docker run --rm -t -e HOST_PORT=8080 \
  -v /your/datacube/level2/dir:/l2dir \
  -p 8080:2741 \
  vudongpham/forcelive forcelive /l2dir
```

<i>Optional arguments:</i>
- `-p` | `--port`: port the app listens on (inside the container). Default: `2741`
- `--host-port`: port shown in the start-up message. Default: `$HOST_PORT`
- `-w` | `--workers`: Number of parallel threads used to read the images. Default: `4 x CPU cores` (max 32). Increase it when the datacube is on a network file system.
- `--log-requests`: also log every HTTP request
- `--debug`: Run Flask in debug mode.

With `-t`, the terminal shows coloured output and a live progress bar; without it, one line per time series. Set `NO_COLOR=1` to turn colours off.

### Features
- **Bands and indices**: Blue, Green, Red, NIR, SWIR1, SWIR2, the Sentinel-2 red-edge bands (RE1–RE3) and broad NIR (B8), NDVI, EVI, NBR, NDMI, NDWI, MNDWI, AWEI, MSI and Tasseled Cap brightness / greenness / wetness. Sensors without the selected band (e.g. Landsat for red edge) are skipped.
- **Cloud mask** (QAI): *None* (only no-data pixels removed), *Default* (cloud, cloud shadow, snow, sub-zero reflectance) or *Aggressive* (Default + high aerosol, saturation, high sun zenith and poor illumination).
- **Line** through the observations, computed in the browser, so switching is instant and needs no re-extraction: linear interpolation, monotone cubic spline, adaptive moving average, adaptive Savitzky–Golay, adaptive RBF ensemble, harmonic regression (basic / advanced / full = 1 / 2 / 3 annual harmonics plus a linear trend, after [Zhu et al. 2015](https://doi.org/10.1016/j.rse.2015.02.009)) or no line. All lines span the whole selected date range.
- **Scene overlay**: click an observation in the chart to show that scene on the map (true color, false color or SWIR). Only the datacube tile of the time series is rendered.
- **Export**: copy the dates / values, download a CSV, or save the chart as PNG / JPEG.
- **Base maps**: satellite, OpenStreetMap or blank. The datacube tile of the selected location is outlined on the map.
- Press **Enter** anywhere to run *View time series*.

### Performance notes
- Images are read in parallel, and only the 1x1 pixel window that is needed gets read. QAI screening and BOA reading happen in a single pass, so cloudy scenes never open their BOA file.
- GDAL directory scanning is disabled (`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`), so opening a file in a tile folder with thousands of files stays fast.
- Every pixel value that is read is cached in memory, and the tile file index is cached too. Switching band, cloud mask or date range at the same location is therefore almost instant.
- A new click cancels the previous request.
- Scene overlay tiles are rendered from decimated reads and cached in memory.


### 3. Open any web browser:

```
http://localhost:2741
```

`Note`: If you are using VPN, makes sure the web-browser is in the same network as the local network!

