'use strict';

const DAY_MS = 24 * 60 * 60 * 1000;
const POLL_MS = 120;          // status poll interval while a job is running
const OPTION_DEBOUNCE_MS = 250;

const COLORS = {
  LND: { face: 'rgb(197, 27, 138)', edge: 'rgb(0, 0, 0)' },
  SEN: { face: 'rgb(35, 139, 69)', edge: 'rgb(0, 0, 0)' },
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

const map = L.map('map', { preferCanvas: true, zoomSnap: 0.5 })
  .setView([52.640564, 13.374481], 11);

L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {
    maxZoom: 19,
    keepBuffer: 4,               // keep more tiles around the view -> smoother panning
    updateWhenZooming: false,
    attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics',
  }
).addTo(map);

let clickMarker = null;
let pulseRing = null;
let tileOutline = null;

function showLocation(latlng) {
  if (clickMarker) {
    clickMarker.setLatLng(latlng);
    pulseRing.setLatLng(latlng);
  } else {
    clickMarker = L.marker(latlng).addTo(map);
    // SVG renderer so the ring has a DOM element we can animate with CSS
    pulseRing = L.circleMarker(latlng, {
      radius: 10, color: '#fff', weight: 2, fill: false, interactive: false,
      renderer: L.svg(),
    }).addTo(map);
  }
}

function setLoading(on) {
  const el = pulseRing && pulseRing.getElement && pulseRing.getElement();
  if (el) el.classList.toggle('pulse', on);
  $('progressWrap').classList.toggle('active', on);
}

function showTile(tile, outline) {
  $('tile').textContent = tile || '';
  if (!outline) return;
  if (tileOutline) {
    tileOutline.setLatLngs(outline);
  } else {
    tileOutline = L.polygon(outline, {
      color: '#ffd400', weight: 2, fill: false, dashArray: '6 4', interactive: false,
    }).addTo(map);
  }
}

// ---------------------------------------------------------------------------
// Chart (created once, then updated in place -> smooth transitions)
// ---------------------------------------------------------------------------

const chart = new Chart($('myChart').getContext('2d'), {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: 'Landsat',
        data: [],
        pointRadius: 5,
        pointHoverRadius: 8,
        backgroundColor: COLORS.LND.face,
        borderColor: COLORS.LND.edge,
        borderWidth: 1,
        order: 1,
      },
      {
        label: 'Sentinel-2',
        data: [],
        pointRadius: 5,
        pointHoverRadius: 8,
        pointStyle: 'triangle',
        backgroundColor: COLORS.SEN.face,
        borderColor: COLORS.SEN.edge,
        borderWidth: 1,
        order: 2,
      },
      {
        label: 'Linear interpolation',
        type: 'line',
        data: [],
        pointRadius: 0,
        pointHoverRadius: 0,
        borderWidth: 1.5,
        borderColor: 'rgba(0, 0, 0, 0.75)',
        tension: 0,
        order: 3,
      },
    ],
  },
  options: {
    parsing: false,     // data is already {x, y} with numeric x
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 350, easing: 'easeOutCubic' },
    transitions: {
      active: { animation: { duration: 120 } },
    },
    interaction: { mode: 'nearest', intersect: false, axis: 'x' },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'day', displayFormats: { day: 'dd-MM-yyyy', month: 'MMM yyyy' } },
        ticks: { maxTicksLimit: 10, autoSkip: true, maxRotation: 0 },
      },
      y: {
        type: 'linear',
        title: { display: true, text: 'NDVI × 10000' },
      },
    },
    plugins: {
      decimation: { enabled: false },
      tooltip: {
        filter: (item) => item.datasetIndex !== 2,
        callbacks: {
          title: (items) => new Date(items[0].parsed.x).toLocaleDateString('en-GB').replaceAll('/', '-'),
          label: (item) => `${item.raw.s || item.dataset.label}: ${item.parsed.y}`,
        },
      },
    },
  },
});

// raw points of the current job, kept sorted by date
let series = [];
let renderQueued = false;

function insertSorted(points) {
  for (const p of points) {
    const pt = { x: p[0] * DAY_MS, y: p[1], s: p[2], d: p[0] };
    // binary search insertion (points mostly arrive in date order -> cheap)
    let lo = 0, hi = series.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (series[mid].x <= pt.x) lo = mid + 1; else hi = mid;
    }
    series.splice(lo, 0, pt);
  }
}

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    render();
  });
}

function render() {
  const lnd = [], sen = [];
  for (const p of series) (p.s.startsWith('LND') ? lnd : sen).push(p);
  const ds = chart.data.datasets;
  ds[0].data = lnd;
  ds[1].data = sen;
  ds[2].data = series.slice();

  const ys = series.map((p) => p.y);
  const minY = ys.length ? Math.min(...ys) : 0;
  chart.options.scales.y.min = minY < 0 ? minY - 300 : 0;
  chart.update();
}

// The old curve stays on screen until the first points of the new job arrive,
// so Chart.js morphs old -> new (e.g. when switching band) instead of blinking.
let pendingReset = null;

function resetChart(startDate, endDate, band) {
  pendingReset = { startDate, endDate, band };
}

function applyReset() {
  const { startDate, endDate, band } = pendingReset;
  pendingReset = null;
  series = [];
  const o = chart.options.scales;
  o.x.min = startDate;
  o.x.max = endDate;
  o.y.title.text = band === 'NDVI' ? 'NDVI × 10000' : `${band} reflectance × 10000`;
}

// initial empty chart spans the selected date range
resetChart($('startDate').value, $('endDate').value, $('Bands').value);
applyReset();
chart.update('none');

// ---------------------------------------------------------------------------
// Jobs: start, stream results, cancel
// ---------------------------------------------------------------------------

let currentJob = null;
let pollTimer = null;

function readOptions() {
  const lat = $('lat').textContent;
  const lng = $('lng').textContent;
  const startDate = $('startDate').value;
  const endDate = $('endDate').value;
  const sensorList = Array.from(
    document.querySelectorAll('#sGroups input[name="selectedSensors"]:checked')
  ).map((el) => el.value);
  const band = $('Bands').value;
  const cloudMask = document.querySelector('input[name="cloudOption"]:checked').value;
  return { lat, lng, startDate, endDate, sensorList, band, cloudMask };
}

function validate(o, silent) {
  let msg = null;
  if (!o.lat || !o.lng) msg = 'No coordinates selected!';
  else if (!o.startDate || !o.endDate) msg = 'Please select dates';
  else if (o.startDate > o.endDate) msg = 'Start date cannot be after End date';
  else if (o.sensorList.length === 0) msg = 'Sensor list cannot be empty';
  if (msg && !silent) alert(msg);
  return !msg;
}

function setStatus(text, isError) {
  const prog = $('prog');
  prog.textContent = text;
  prog.classList.toggle('error', !!isError);
}

function setProgress(current, total) {
  const f = total > 0 ? current / total : 0;
  $('progressBar').style.transform = `scaleX(${f})`;
}

function setExportEnabled(on) {
  ['copyXbutton', 'copyYbutton', 'csvButton'].forEach((id) => { $(id).disabled = !on; });
}

async function startJob(silent) {
  const o = readOptions();
  if (!validate(o, silent)) return;

  const previous = currentJob;
  currentJob = null;
  clearTimeout(pollTimer);

  resetChart(o.startDate, o.endDate, o.band);
  setExportEnabled(false);
  setProgress(0, 1);
  setLoading(true);
  setStatus('Preparing data…');

  let data;
  try {
    const r = await fetch('/sendData', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...o, cancel: previous }),
    });
    data = await r.json();
  } catch (err) {
    console.error('Error starting job:', err);
    setLoading(false);
    setStatus('Failed to start processing on the server.', true);
    return;
  }

  showTile(data.tile, data.outline);
  if (!data.job) {
    setLoading(false);
    setStatus(data.name || 'Error while processing', true);
    return;
  }
  currentJob = data.job;
  handleStatus(data, 0);
}

function handleStatus(data, cursor) {
  if (data.job !== currentJob) return;   // stale response of a superseded job

  const hasPoints = data.points && data.points.length;
  if (pendingReset && (hasPoints || data.status !== 'running')) {
    applyReset();
    scheduleRender();
  }
  if (hasPoints) {
    insertSorted(data.points);
    scheduleRender();
  }
  cursor = data.cursor;
  setProgress(data.current, data.total);

  if (data.status === 'running') {
    setStatus(`${data.name}: ${data.current} / ${data.total} images · ${data.valid} clear`);
    pollTimer = setTimeout(() => poll(data.job, cursor), POLL_MS);
    return;
  }

  setLoading(false);
  if (data.status === 'done') {
    const t = data.elapsed != null ? ` in ${data.elapsed.toFixed(2)} s` : '';
    setStatus(data.total
      ? `${data.valid} clear observations out of ${data.total} images${t}`
      : (data.name || 'No images found'));
    setExportEnabled(series.length > 0);
  } else if (data.status === 'error') {
    setStatus(data.name || 'Error while processing', true);
  }
}

function poll(jobId, cursor) {
  fetch(`/status/${jobId}?since=${cursor}`)
    .then((r) => r.json())
    .then((data) => handleStatus(data, cursor))
    .catch((err) => {
      if (jobId !== currentJob) return;
      console.error(err);
      setLoading(false);
      setStatus('Lost connection to the server.', true);
    });
}

// ---------------------------------------------------------------------------
// UI wiring
// ---------------------------------------------------------------------------

map.on('click', (e) => {
  $('lat').textContent = e.latlng.lat.toFixed(6);
  $('lng').textContent = e.latlng.lng.toFixed(6);
  showLocation(e.latlng);

  if ($('autoRun').checked) {
    startJob(true);
  } else {
    fetch(`/tile?lat=${e.latlng.lat}&lng=${e.latlng.lng}`)
      .then((r) => r.json())
      .then((d) => showTile(d.tile, d.outline))
      .catch(() => {});
  }
});

$('getData').addEventListener('click', () => startJob(false));

// re-run automatically when an option changes (cached pixels make this instant)
let optionTimer = null;
function onOptionChange() {
  if (!$('autoRun').checked || !$('lat').textContent) return;
  clearTimeout(optionTimer);
  optionTimer = setTimeout(() => startJob(true), OPTION_DEBOUNCE_MS);
}
document.querySelectorAll('#info-panel select, #info-panel input:not(#autoRun)')
  .forEach((el) => el.addEventListener('change', onOptionChange));

// export buttons: one listener each, reading the *current* data on click
function flash(button, text) {
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.textContent = text;
  button.classList.add('copied');
  setTimeout(() => {
    button.textContent = original;
    button.classList.remove('copied');
  }, 1000);
}

async function copyArray(button, values) {
  try {
    await navigator.clipboard.writeText(`[${values.join(', ')}]`);
    flash(button, 'Copied!');
  } catch (err) {
    console.error('Copy failed:', err);
  }
}

$('copyXbutton').addEventListener('click', (e) => copyArray(e.currentTarget, series.map((p) => p.d)));
$('copyYbutton').addEventListener('click', (e) => copyArray(e.currentTarget, series.map((p) => p.y)));

$('csvButton').addEventListener('click', (e) => {
  const o = readOptions();
  const rows = ['date,days_since_epoch,sensor,' + o.band];
  for (const p of series) {
    rows.push(`${new Date(p.x).toISOString().slice(0, 10)},${p.d},${p.s},${p.y}`);
  }
  const blob = new Blob([rows.join('\n') + '\n'], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `forcelive_${$('tile').textContent || 'tile'}_${o.lat}_${o.lng}_${o.band}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  flash(e.currentTarget, 'Saved!');
});
