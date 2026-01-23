// Initialize map (example center: London)
var map = L.map('map').setView([52.640564, 13.374481], 11);

// Tile layer
L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 
  {
    maxZoom: 19,
    attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics'
  }
).addTo(map);


// Marker variable
var clickMarker = null;
var x_data = [];
var y_data = [];
var chartInstance = null;  

// Reusable function to send data to Flask
function sendData(lat, lng, startDate, endDate, sensorList, band, cloudMask) {
  return fetch('/sendData', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      lat: lat,
      lng: lng,
      startDate: startDate,
      endDate: endDate,
      sensorList: sensorList,
      band: band,
      cloudMask: cloudMask
    })
  })
  .then(response => response.json())
  .then(data => {
    console.log("Job started:", data);
    return data; // caller can still use .then(data => ...)
  })
  .catch(err => {
    console.error("Error starting job:", err);
    throw err;
  });
}



// function sensorSelection() {
//   const optA = document.getElementById("allSensors");
//   const optB = document.getElementById("selectedSensors");
//   const bGroup = document.getElementById("sGroups");
//   const bChecks = bGroup.querySelectorAll("input[type=checkbox]");

//   function update() {
//     if (optB.checked) {
//       bGroup.classList.remove("disabled");
//       bChecks.forEach(c => c.disabled = false);
//     } else {
//       bGroup.classList.add("disabled");
//       bChecks.forEach(c => c.checked = true);
//       bChecks.forEach(c => c.disabled = true);
//     }
//   }

//   optA.addEventListener("change", update);
//   optB.addEventListener("change", update);
//   update();
// }

// sensorSelection();


// Click event
map.on('click', function (e) {
  var lat = e.latlng.lat.toFixed(6);
  var lng = e.latlng.lng.toFixed(6);

  // update panel
  document.getElementById("lat").textContent  = lat;
  document.getElementById("lng").textContent  = lng;

  // marker logic
  if (clickMarker) {
    clickMarker.setLatLng(e.latlng);
  } else {
    clickMarker = L.marker(e.latlng).addTo(map);
  }
});

// When user clicks the button
document.getElementById("getData").addEventListener("click", function () {
  var lat = document.getElementById("lat").textContent;
  var lng = document.getElementById("lng").textContent;

  if (!lat || !lng) {
    alert("No coordinates selected!")
    return;
  }

  var startDate = document.getElementById("startDate").value;
  var endDate = document.getElementById("endDate").value;

  if ((startDate === "") || (endDate === "")) {
    alert("Please select dates");
    return;
  }

  if (startDate > endDate) {
    alert("Start date cannot be after End date");
    return;
  }

  var sensorList = Array.from(
    document.querySelectorAll('#sGroups input[name="selectedSensors"]:checked')
  ).map(el => el.value);

  if (sensorList.length === 0) {
    alert("Sensor list cannot be empty");
    return;
  }

  var band = document.getElementById("Bands").value;

  const cloudMask = document.querySelector('input[name="cloudOption"]:checked').value;

  // start processing on the server and then poll progress
  sendData(lat, lng, startDate, endDate, sensorList, band, cloudMask)
    .then(() => {
      poll();
    })
    .catch(err => {
      console.error(err);
      alert("Failed to start processing on the server.");
    });

});


function poll() {
  const prog = document.getElementById("prog");
  const spinner = document.getElementById("spinner");

  // show spinner + progress text
  spinner.style.display = "block";
  prog.style.display = "block";

  const id = setInterval(() => {
    fetch("/progress")
      .then(r => r.json())
      .then(data => {
        prog.textContent = `${data.name}: ${data.current} / ${data.total}`;

        if (data.status === "done") {
          clearInterval(id);

          // hide spinner + text
          spinner.style.display = "none";
          prog.style.display = "none";

          // fetch results once the job is done and plot them
          fetch("/results")
            .then(r => r.json())
            .then(result => {
              x_data = result.xdata || [];
              y_data = result.ydata || [];
              // console.log(x_data);
              // console.log(y_data);
              plotChart();
            });
        } else if (data.status === "error") {
          clearInterval(id);
          spinner.style.display = "none";
          prog.textContent = data.name || "Error while processing";
        }
      })
      .catch(() => {
        clearInterval(id);
        spinner.style.display = "none";
        prog.style.display = "none";
      });
  }, 300);
}


function daysToDate(days) {
  return new Date(days * 24 * 60 * 60 * 1000);
}


function plotChart(){
  const x = x_data;
  const x_date = x.map(daysToDate);
  const y = y_data;

  const min_x_axis = document.getElementById("startDate").value;
  const max_x_axis = document.getElementById("endDate").value;

  const min_y = Math.min(y);

  var min_y_axis = 0;
  var max_y_axis = 10000;

  if (min_y < 0) {
    min_y_axis = min_y;
  }

  // Convert arrays into Chart.js scatter format
  const points = x_date.map((value, i) => ({ x: value, y: y[i] }));

  const ctx = document.getElementById('myChart').getContext('2d');

  // destroy old chart if it exists
  if (chartInstance) {
    chartInstance.destroy();
  }

  // create new chart and keep reference
  chartInstance = new Chart(ctx, {
    type: 'scatter',
    data: {
      datasets: [{
        label: 'Clear observations',
        data: points,
        pointRadius: 6,
        backgroundColor: 'rgb(197, 27, 138)', // face color
        borderColor: 'rgb(0, 0, 0)',           // edge color
        borderWidth: 1,
      },

      {
        label: 'Linear interpolation',
        type: 'line',
        data: points,          // same x/y points
        parsing: false,        // optional; keeps {x,y} as-is
        showLine: true,
        pointRadius: 0,        // no markers on the line
        borderWidth: 2,
        borderColor: 'rgb(0,0,0)',
        tension: 0             // 0 = straight segments; try 0.3 for smoothing
      }

    ]
    },
    options: {
      scales: {
        x: {
          type: 'time',
          min: min_x_axis,
          max: max_x_axis,
          time: {
            unit: 'day',
            displayFormats: {
              day: 'dd-MM-yyyy'
            }
          },
          ticks: {
            maxTicksLimit: 10,
            autoSkip: false
          }
        },
        y: {
          type: 'linear',
          min: min_y_axis
        }
      },
      plugins: {
        tooltip: {
          callbacks: {
            title: function (items) {
              const x = items[0].parsed.x;
              return new Date(x)
                .toLocaleDateString('en-GB')
                .replaceAll('/', '-');
            },
            label: function (item) {
              return 'Value: ' + item.parsed.y;
            }
          }
        }
      }
    }
  });
}
