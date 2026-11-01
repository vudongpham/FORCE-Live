# FORCE Live
- Web-based visualizing time-series data from FORCE datacube
- Data extraction and cloud masking are performed on-the-fly
  
![a](https://github.com/user-attachments/assets/0c11c175-2097-4cd4-9a4a-195c851052f6)

### 1. Installation
Pull the docker image
```
docker pull vudongpham/forcelive:latest
```


### 2. Run

```
docker run --rm \
  -v /your/datacube/level2/dir:/l2dir \
  -p 2741:2741 \
  vudongpham/forcelive forcelive /l2dir
```
Replace the `/your/datacube/level2/dir` with your dir!

<i>Optional arguments:</i>
- `-p` | `--port`: Local port which will be used for hosting the app. Default: `2741` (You must change the docker '-p' correspondigly when changing this)
- `-w` | `--workers`: Number of parallel threads used to read the images. Default: `4 x CPU cores` (max 32). Increase it when the datacube is on a network file system.
- `--debug`: Run Flask in debug mode.

### Performance notes
- Images are read in parallel, and only the 1x1 pixel window that is needed gets read. QAI screening and BOA reading happen in a single pass, so cloudy scenes never open their BOA file.
- GDAL directory scanning is disabled (`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`), so opening a file in a tile folder with thousands of files stays fast.
- Every pixel value that is read is cached in memory, and the tile file index is cached too. Switching band, cloud mask or date range at the same location is therefore almost instant.
- Results stream into the chart while they are being read. A new click cancels the previous request.


### 3. Open any web browser:

```
http://localhost:2741
```

`Note`: If you are using VPN, makes sure the web-browser is in the same network as the local network!

