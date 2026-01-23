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
  -v /your/datacube/level2/dir:/l2dir
  -p 2741:2741 \
  vudongpham/forcelive forcelive /l2dir
```


<i>Required arguments:</i>
- `l2dir` \
  FORCE datacube Level-2 directory path, the "datacube-definition.prj" file MUST exist in this directory

<i>Optional arguments:</i>
- `-p` | `--port`: Local port which will be used for hosting the app. Default: `2741` (You must change the docker '-p' correspondigly when changing this)


### 3. Open any web browser:

```
http://localhost:2741
```

`Note`: If you are using VPN, makes sure the web-browser is in the same network as the local network!

