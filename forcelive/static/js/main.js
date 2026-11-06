'use strict';

const DAY_MS = 24 * 60 * 60 * 1000;
// y axis: 110 % of the highest / lowest value (rounded outwards to 100),
// within +/-10000 (reflectance / index x 10000). Only when values go beyond
// +/-10000 does an outlier fence decide how far the axis extends (see
// axisRange).
const Y_LIMIT = 10000;
const Y_FENCE = 3;            // fence: quartiles -/+ 3 x interquartile range
const Y_FENCE_MIN_OBS = 8;    // fewer observations: no fence
const Y_PAD = 1.1;
const Y_ROUND = 100;
const INDEX_BANDS = new Set(['NDVI', 'EVI', 'NBR', 'NDMI', 'NDWI', 'MNDWI', 'AWEI', 'MSI', 'TCB', 'TCG', 'TCW']);
const BAND_TITLES = { TCB: 'TC Brightness', TCG: 'TC Greenness', TCW: 'TC Wetness' };
const POLL_MS = 120;          // status poll interval while a job is running

// Landsat is magenta and Sentinel-2 green
const COLORS = {
  LND: { face: 'rgb(197, 27, 138)', edge: 'rgb(0, 0, 0)' },
  SEN: { face: 'rgb(35, 139, 69)', edge: 'rgb(0, 0, 0)' },
};

const $ = (id) => document.getElementById(id);

// sensor code -> label shown in the sensor checkboxes (e.g. "Sentinel-2A")
const SENSOR_LABELS = Object.fromEntries(
  Array.from(document.querySelectorAll('input[name="selectedSensors"]'))
    .map((el) => [el.value, el.parentElement.textContent.trim()]));

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

const map = L.map('map', { preferCanvas: true, zoomSnap: 0.5 })
  .setView([52.640564, 13.374481], 11);

// Base maps, switchable in the top-left corner. The choice is remembered in
// this browser. The scene overlay always stays on top.
const TILE_OPTIONS = {
  maxZoom: 19,
  keepBuffer: 4,               // keep more tiles around the view -> smoother panning
  updateWhenZooming: false,
};
const BASE_MAPS = {
  Satellite: L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { ...TILE_OPTIONS, attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics' }),
  OpenStreetMap: L.tileLayer(
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { ...TILE_OPTIONS, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }),
  Blank: L.layerGroup(),       // no tiles: the map background is white
};
const BASE_MAP_KEY = 'forcelive.baseMap';

function storedBaseMap() {
  try {
    const name = localStorage.getItem(BASE_MAP_KEY);
    return BASE_MAPS[name] ? name : 'Satellite';
  } catch (err) {
    return 'Satellite';
  }
}

BASE_MAPS[storedBaseMap()].addTo(map);
map.getContainer().classList.toggle('blank-map', storedBaseMap() === 'Blank');
L.control.layers(BASE_MAPS, null, { position: 'topleft', collapsed: false }).addTo(map);
map.on('baselayerchange', (e) => {
  map.getContainer().classList.toggle('blank-map', e.name === 'Blank');
  try { localStorage.setItem(BASE_MAP_KEY, e.name); } catch (err) { /* not stored */ }
});

let clickMarker = null;
let pulseRing = null;
let overlay = null;          // scene shown on the map
let tileOutline = null;      // datacube tile of the selected location

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

// name and outline of the datacube tile of the selected location
function showTile(tile, outline, exists = true) {
  const el = $('tile');
  el.textContent = tile || '–';
  el.classList.toggle('missing', !!tile && !exists);
  el.title = tile && !exists
    ? `${tile} is not in the datacube`
    : 'Datacube tile of the selected location (outlined on the map)';
  if (!outline) return;
  if (tileOutline) {
    tileOutline.setLatLngs(outline);
  } else {
    tileOutline = L.polygon(outline, {
      color: '#ffd400', weight: 2, fill: false, dashArray: '6 4', interactive: false,
    }).addTo(map);
  }
}

function setLoading(on) {
  const el = pulseRing && pulseRing.getElement && pulseRing.getElement();
  if (el) el.classList.toggle('pulse', on);
  $('progressWrap').classList.toggle('active', on);
  $('stopButton').disabled = !on;
}

// ---------------------------------------------------------------------------
// Scene overlay: clicking an observation in the chart renders that scene
// (map tiles made by the server from the BOA file of that date in the
// datacube tile of the time series only) on top of the basemap.
// ---------------------------------------------------------------------------

let overlayPoint = null;     // observation currently shown
let seriesTile = null;       // datacube tile of the current time series

function fmtDate(ms) {
  return new Date(ms).toLocaleDateString('en-GB').replaceAll('/', '-');
}

function setOverlayInfo(text, isError) {
  const el = $('overlayInfo');
  el.textContent = text;
  el.classList.toggle('error', !!isError);
}

// Progress of the overlay: a box on the map and a bar under the overlay
// controls, while the server renders the tiles of the current view.
const OverlayStatus = L.Control.extend({
  options: { position: 'topright' },
  onAdd() {
    const div = L.DomUtil.create('div', 'overlay-status');
    div.hidden = true;
    div.innerHTML = '<span class="spinner"></span><span class="text"></span>';
    L.DomEvent.disableClickPropagation(div);
    return div;
  },
});
const overlayStatus = new OverlayStatus().addTo(map);

function setOverlayProgress(text, fraction) {
  const box = overlayStatus.getContainer();
  const wrap = $('ovProgressWrap');
  const bar = $('ovProgressBar');
  if (text == null) {
    box.hidden = true;
    wrap.classList.remove('active', 'indeterminate');
    bar.style.transform = 'scaleX(0)';
    return;
  }
  box.hidden = false;
  box.querySelector('.text').textContent = text;
  wrap.classList.add('active');
  wrap.classList.toggle('indeterminate', fraction == null);
  bar.style.transform = `scaleX(${fraction == null ? 1 : fraction})`;
}

// count the tiles of the overlay layer while they load
function trackTiles(layer, what) {
  let total = 0, done = 0, failed = 0;
  const update = () => {
    const pct = total ? Math.round((100 * done) / total) : 0;
    setOverlayProgress(`Rendering ${what}: ${pct} % (${done} / ${total} tiles)`, total ? done / total : 0);
  };
  layer.on('loading', () => { total = 0; done = 0; failed = 0; update(); });
  layer.on('tileloadstart', () => { total++; update(); });
  layer.on('tileload', () => { done++; update(); });
  layer.on('tileerror', () => { done++; failed++; update(); });
  layer.on('load', () => {
    setOverlayProgress(null);
    if (layer !== overlay) return;
    if (failed) setOverlayInfo(`${what}: ${failed} of ${total} tiles failed to load`, true);
  });
}

function removeOverlay() {
  overlayPoint = null;
  if (overlay) { map.removeLayer(overlay); overlay = null; }
  $('clearOverlay').disabled = true;
  updateOverlayCard();
  setOverlayInfo('');
  setOverlayProgress(null);
  chart.update('none');
}

function showOverlay(p) {
  overlayPoint = p;
  $('clearOverlay').disabled = false;
  updateOverlayCard();
  const what = `${SENSOR_LABELS[p.s] || p.s} ${fmtDate(p.x)}`;
  chart.update('none');
  // a new layer per scene, so tile events of the previous scene cannot
  // be mixed into the progress of this one
  if (overlay) map.removeLayer(overlay);
  const params = new URLSearchParams({ tile: seriesTile, scene: p.id, vis: $('visMode').value });
  overlay = L.tileLayer(`/overlay/{z}/{x}/{y}.png?${params}`, {
    maxZoom: 19, opacity: Number($('opacity').value), zIndex: 10,
  });
  trackTiles(overlay, what);
  overlay.addTo(map);
  setOverlayInfo(`${what} · ${seriesTile}/${p.id}`);
}

// ---------------------------------------------------------------------------
// Chart (created once, then updated in place -> smooth transitions)
// ---------------------------------------------------------------------------

// Hover/tooltip interaction that only considers the observation datasets
// (Landsat, Sentinel-2) and never the line: with dense lines (harmonic fits,
// moving average) the line would otherwise almost always be the nearest
// element, leaving the tooltip with nothing to show.
// The nearest observation is the one closest to the mouse in screen pixels
// (straight-line distance in x and y), not just the closest date.
const LINE_DS = 2;
Chart.Interaction.modes.nearestObservation = (chart, e) => {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  let best = [], bestDist = Infinity;
  chart.data.datasets.forEach((ds, di) => {
    if (di === LINE_DS || !chart.isDatasetVisible(di)) return;
    chart.getDatasetMeta(di).data.forEach((el, index) => {
      if (!el || el.skip) return;
      const area = chart.chartArea;
      if (el.y < area.top || el.y > area.bottom) return;   // cut off: not visible
      const d = Math.hypot(el.x - pos.x, el.y - pos.y);
      if (d < bestDist) { bestDist = d; best = [{ element: el, datasetIndex: di, index }]; }
      else if (d === bestDist) best.push({ element: el, datasetIndex: di, index });
    });
  });
  return best;
};

// the observation shown as map overlay is drawn larger, with a thick
// bright-yellow edge that stands out against all point colours
const SELECTED_EDGE = '#ffd400';
const isSelected = (ctx) => !!ctx.raw && ctx.raw === overlayPoint;
function highlightRadius(ctx) { return isSelected(ctx) ? 9 : 5; }
function highlightEdge(ctx) { return isSelected(ctx) ? SELECTED_EDGE : 'rgb(0, 0, 0)'; }
function highlightEdgeWidth(ctx) { return isSelected(ctx) ? 3 : 1; }

const chart = new Chart($('myChart').getContext('2d'), {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: 'Landsat',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.LND.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
        order: 1,
      },
      {
        label: 'Sentinel-2',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.SEN.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
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
    interaction: { mode: 'nearestObservation', intersect: false },
    onClick: (e, elements) => {
      if (!elements.length) return;
      const { datasetIndex, index } = elements[0];
      const p = chart.data.datasets[datasetIndex].data[index];
      if (p && p.id) showOverlay(p);
    },
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
      legend: {
        labels: {
          filter: (item) => item.datasetIndex !== LINE_DS || $('lineMode').value !== 'none',
        },
      },
      tooltip: {
        filter: (item) => item.datasetIndex !== LINE_DS,
        callbacks: {
          title: (items) => (items.length ? fmtDate(items[0].parsed.x) : ''),
          label: (item) => `${SENSOR_LABELS[item.raw.s] || item.raw.s || item.dataset.label}: ${item.parsed.y}`,
          footer: () => 'Click to show this scene on the map',
        },
      },
    },
  },
});

// raw points of the current job, kept sorted by date. Points are collected
// silently while the extraction runs; the chart is drawn only once it ends.
let series = [];

function insertSorted(points) {
  for (const p of points) {
    const pt = { x: p[0] * DAY_MS, y: p[1], s: p[2], d: p[0], id: p[3] };
    // binary search insertion (points mostly arrive in date order -> cheap)
    let lo = 0, hi = series.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (series[mid].x <= pt.x) lo = mid + 1; else hi = mid;
    }
    series.splice(lo, 0, pt);
  }
}

// ---------------------------------------------------------------------------
// Line modes: how the line through the observations is drawn. All are O(n)
// on the already sorted series, so switching is instant.
//
// Every line spans the whole selected date range (start to end date), not
// only the first to the last clear observation: the series is copied N
// calendar years back and forward (N = length of the date range rounded up
// to whole years, e.g. a 2018-2020 series gets copies in 2015-2017 and
// 2021-2023), the line is computed on the extended series and cut at the
// start and end dates (see Smoothing.extendPeriodic). Only the real
// observations are drawn as points and exported.
// ---------------------------------------------------------------------------

// The moving average, Savitzky–Golay and RBF lines are adaptive: their
// windows / kernel widths follow the density and distribution of the clear
// observations (see static/js/smoothing.js).
const solve = Smoothing.solve;

const days = (v) => `${Math.round(v)} d`;

// line modes whose curve comes with the parameters it used (shown in the legend)
function adaptiveMode(fit, describe) {
  return { tension: 0, adaptive: true, fit, describe };
}

// Harmonic regression (Zhu et al. 2015, https://doi.org/10.1016/j.rse.2015.02.009):
//   y = c0 + c1 cos(wt) + c2 sin(wt) + c3 t            (basic)
//       + c4 cos(2wt) + c5 sin(2wt)                     (advanced)
//       + c6 cos(3wt) + c7 sin(3wt)                     (full)
// with w = 2*pi / 365.25 and t in days. Fitted by least squares on the
// extended series (observations and their copies) and evaluated on a daily
// grid from the start to the end date.
const HARMONIC_T = 365.25;
const HARMONIC_ORDERS = { basic: 1, advanced: 2, full: 3 };
const HARMONIC_MAX_SAMPLES = 2000;   // max evaluation points (keeps long series fast)

function harmonicRow(t, nHarm) {
  // t is in days relative to the start date (better conditioning than
  // days since 1970; the shift is absorbed by the intercept and the phases).
  // The trend column is in years so all columns have similar magnitude; this
  // only rescales its coefficient, the fitted curve is identical.
  const w = 2 * Math.PI / HARMONIC_T;
  const row = [1, Math.cos(w * t), Math.sin(w * t), t / HARMONIC_T];
  for (let k = 2; k <= nHarm; k++) row.push(Math.cos(k * w * t), Math.sin(k * w * t));
  return row;
}

function harmonicFit(pts, model, range) {
  const n = pts.length;
  const nHarm = HARMONIC_ORDERS[model];
  const m = 2 + 2 * nHarm;            // number of coefficients
  if (n < m) return null;             // under-determined: not enough observations

  const t0 = range.from;
  const A = Array.from({ length: m }, () => new Float64Array(m));
  const b = new Float64Array(m);
  for (const p of pts) {
    const r = harmonicRow((p.x - t0) / DAY_MS, nHarm);
    for (let i = 0; i < m; i++) {
      b[i] += r[i] * p.y;
      for (let j = i; j < m; j++) A[i][j] += r[i] * r[j];
    }
  }
  for (let i = 0; i < m; i++) for (let j = 0; j < i; j++) A[i][j] = A[j][i];
  const c = solve(A, b);
  if (!c || !c.every(Number.isFinite)) return null;

  const spanDays = (range.to - t0) / DAY_MS;
  const step = Math.max(1, Math.ceil(spanDays / HARMONIC_MAX_SAMPLES));
  const value = (d) => {
    const r = harmonicRow(d, nHarm);
    let y = 0;
    for (let i = 0; i < m; i++) y += c[i] * r[i];
    return { x: t0 + d * DAY_MS, y: Math.round(y) };
  };
  const out = [];
  for (let d = 0; d <= spanDays; d += step) out.push(value(d));
  if (out[out.length - 1].x < range.to) out.push(value(spanDays));   // end exactly at the end date
  return out;
}

function harmonicMode(model, label) {
  return {
    label,
    tension: 0,
    data: (ext, range) => harmonicFit(ext, model, range) || [],
    // shown in the status line when the fit is not possible
    minObs: 2 + 2 * HARMONIC_ORDERS[model],
  };
}

// the observations within the range plus `k` on each side beyond it: the
// chart cuts the line off at the axis (= start and end date), and the
// points beyond give the monotone cubic its proper shape at both ends
function withMargin(pts, range, k) {
  let lo = pts.findIndex((p) => p.x >= range.from);
  if (lo < 0) lo = pts.length;
  let hi = pts.length;
  while (hi > 0 && pts[hi - 1].x > range.to) hi--;
  return pts.slice(Math.max(0, lo - k), Math.min(pts.length, hi + k));
}

const LINE_MODES = {
  linear: { label: 'Linear interpolation', tension: 0,
            data: (ext, range) => Smoothing.clipCurve(ext, range.from, range.to) },
  // Chart.js monotone cubic interpolation: smooth, passes through every
  // observation and never overshoots between them
  spline: { label: 'Spline (monotone cubic)', cubic: 'monotone',
            data: (ext, range) => withMargin(ext, range, 2) },
  movavg: adaptiveMode(
    (ext, range) => Smoothing.movingAverage(ext, range),
    (p) => `Moving average (adaptive: window ±${days(p.hMin)}–${days(p.hMax)})`),
  savgol: adaptiveMode(
    (ext, range) => Smoothing.savitzkyGolay(ext, range),
    (p) => `Savitzky–Golay (adaptive, robust: window ±${days(p.hMin)}–${days(p.hMax)})`),
  rbf: adaptiveMode(
    (ext, range) => Smoothing.rbf(ext, range),
    (p) => `RBF ensemble (σ = ${p.sigmas.join('/')} d)`),
  harmBasic:    harmonicMode('basic', 'Harmonic basic'),
  harmAdvanced: harmonicMode('advanced', 'Harmonic advanced'),
  harmFull:     harmonicMode('full', 'Harmonic full'),
  none:   { label: 'Line', tension: 0, data: () => [] },
};

// date range of the current job (ms, UTC days like the observations); the
// lines are drawn from `from` to `to`
let lineRange = null;

function currentRange() {
  if (lineRange) return lineRange;
  return series.length ? { from: series[0].x, to: series[series.length - 1].x } : null;
}

function applyLineMode(ds) {
  const key = $('lineMode').value;
  const mode = LINE_MODES[key] || LINE_MODES.linear;
  ds.cubicInterpolationMode = mode.cubic || 'default';
  ds.tension = mode.tension || 0;
  const range = currentRange();
  // observations with their copies before and after the date range
  const ext = range ? Smoothing.extendPeriodic(series, range.from, range.to) : [];
  // the minimum number of observations counts the real ones only
  const minObs = mode.adaptive ? 3 : mode.minObs || 1;
  const enough = series.length >= minObs;
  if (mode.adaptive) {
    // the curve has null values in gaps: the line is broken there
    const { data, params } = enough ? mode.fit(ext, range) : { data: [], params: null };
    ds.data = Smoothing.clipCurve(data, range ? range.from : 0, range ? range.to : 0);
    ds.label = params ? mode.describe(params) : 'Line (needs ≥ 3 obs.)';
  } else {
    ds.label = mode.label;
    ds.data = enough ? mode.data(ext, range) : [];
    if (mode.minObs && series.length && !ds.data.length) {
      ds.label += ` (needs ≥ ${mode.minObs} obs.)`;
    }
  }
  // also overrides a legend click that had hidden the line
  chart.setDatasetVisibility(LINE_DS, key !== 'none');
}

function render() {
  const lnd = [], sen = [];
  for (const p of series) (p.s.startsWith('LND') ? lnd : sen).push(p);
  const ds = chart.data.datasets;
  ds[0].data = lnd;
  ds[1].data = sen;
  applyLineMode(ds[LINE_DS]);

  const { min, max } = axisRange(series.map((p) => p.y));
  chart.options.scales.y.min = min;
  chart.options.scales.y.max = max;
  showAxisNote(series, min, max);
  chart.update();
}

// below the chart: how many observations lie outside the y axis (outliers
// cut off by axisRange), so they are not silently missing
function showAxisNote(pts, min, max) {
  let above = 0, below = 0, top = -Infinity, bottom = Infinity;
  for (const p of pts) {
    if (p.y > max) { above++; if (p.y > top) top = p.y; }
    else if (p.y < min) { below++; if (p.y < bottom) bottom = p.y; }
  }
  const n = (k) => `${k} value${k === 1 ? '' : 's'}`;
  const parts = [];
  if (above) parts.push(`${n(above)} above the axis (up to ${top})`);
  if (below) parts.push(`${n(below)} below the axis (down to ${bottom})`);
  const note = $('axisNote');
  note.hidden = !parts.length;
  note.textContent = parts.length
    ? `Outliers not shown: ${parts.join(', ')}. They are still in the copied values and the CSV.`
    : '';
}

// value at quantile q (0..1) of sorted values, linearly interpolated
function quantile(sorted, q) {
  const pos = (sorted.length - 1) * q;
  const i = Math.floor(pos);
  return i + 1 < sorted.length ? sorted[i] + (sorted[i + 1] - sorted[i]) * (pos - i) : sorted[i];
}

// Axis range: 0 .. 110 % of the highest value for positive data, down to
// 110 % of the lowest value when there are negative values, cut off at
// +/-10000. Values within +/-10000 are therefore always shown.
// Only when values go beyond +/-10000 (e.g. MSI, AWEI, or a spike from an
// undetected cloud or a tiny index denominator) does the axis extend past
// it, as far as the outlier fence (Tukey) allows: the 75th percentile
// + 3 x IQR above, the 25th percentile - 3 x IQR below (IQR = interquartile
// range of all clear observations). Regular values beyond +/-10000 are
// shown; isolated spikes beyond the fence are cut off (not drawn, cannot be
// hovered, counted in the note below the chart).
function axisRange(ys) {
  if (!ys.length) return { min: 0, max: Y_LIMIT };
  const sorted = Float64Array.from(ys).sort();
  let lo = sorted[0], hi = sorted[sorted.length - 1];
  let hiFence = Infinity, loFence = -Infinity;
  if (sorted.length >= Y_FENCE_MIN_OBS) {
    const q1 = quantile(sorted, 0.25), q3 = quantile(sorted, 0.75);
    const iqr = q3 - q1;
    if (iqr > 0) {
      hiFence = q3 + Y_FENCE * iqr;
      loFence = q1 - Y_FENCE * iqr;
    }
  }
  const up = (v) => Math.ceil((v * Y_PAD) / Y_ROUND) * Y_ROUND;
  const down = (v) => Math.floor((v * Y_PAD) / Y_ROUND) * Y_ROUND;
  let max = 0, min = 0;
  if (hi > Y_LIMIT) {
    hi = Math.min(hi, Math.max(Y_LIMIT, hiFence));
    max = hi > Y_LIMIT ? up(hi) : Y_LIMIT;
  } else if (hi > 0) {
    max = Math.min(Y_LIMIT, up(hi));
  }
  if (lo < -Y_LIMIT) {
    lo = Math.max(lo, Math.min(-Y_LIMIT, loFence));
    min = lo < -Y_LIMIT ? down(lo) : -Y_LIMIT;
  } else if (lo < 0) {
    min = Math.max(-Y_LIMIT, down(lo));
  }
  return { min, max: max > min ? max : min + Y_ROUND };
}

function showChart(on) {
  $('chartWrap').hidden = !on;
  if (on) chart.resize();
}

// after "View time series", bring the chart into view if it is outside
// the visible part of the panel
function revealChart() {
  const panel = $('info-panel');
  const box = $('chartWrap').getBoundingClientRect();
  const view = panel.getBoundingClientRect();
  if (box.top < view.top || box.bottom > view.bottom) {
    $('chartWrap').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function resetChart(startDate, endDate, band) {
  series = [];
  $('axisNote').hidden = true;
  // drop any hovered point of the previous location: its index may not exist
  // in the new data
  chart.setActiveElements([]);
  chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  const o = chart.options.scales;
  o.x.min = startDate;
  o.x.max = endDate;
  lineRange = { from: Date.parse(startDate), to: Date.parse(endDate) };
  const name = BAND_TITLES[band] || band;
  o.y.title.text = INDEX_BANDS.has(band) ? `${name} × 10000` : `${name} reflectance × 10000`;
  showChart(false);
}

// draw the collected points (called once, when the extraction has ended)
function showResults() {
  showChart(true);
  render();
  revealChart();
}

// ---------------------------------------------------------------------------
// Jobs: start, stream results, cancel
// ---------------------------------------------------------------------------

let currentJob = null;
let pollTimer = null;

function readOptions() {
  const lat = $('lat').value.trim();
  const lng = $('lng').value.trim();
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
  else if ($('Bands').selectedOptions[0].hasAttribute('data-s2only')
           && !o.sensorList.some((s) => s.startsWith('SEN'))) {
    msg = `${o.band} exists for Sentinel-2 only: please select a Sentinel-2 sensor`;
  }
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
  removeOverlay();

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
  seriesTile = data.tile || null;
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

  if (data.points && data.points.length) insertSorted(data.points);
  cursor = data.cursor;
  setProgress(data.current, data.total);

  if (data.status === 'running') {
    setStatus(`${data.name}: ${data.current} / ${data.total} images · ${data.valid} clear`);
    pollTimer = setTimeout(() => poll(data.job, cursor), POLL_MS);
    return;
  }

  setLoading(false);
  const t = data.elapsed != null ? ` in ${data.elapsed.toFixed(2)} s` : '';
  const failed = data.failed
    ? ` · ${data.failed} of ${data.total} files could not be read (${data.error})` : '';
  if (data.status === 'done') {
    let msg;
    if (!data.total) msg = data.name || 'No images found';
    else if (!data.images) msg = `No images found${t}${failed}`;
    else msg = `${data.valid} clear observations out of ${data.images} images${t}${failed}`;
    setStatus(msg, !!data.failed);
    showResults();
    setExportEnabled(series.length > 0);
  } else if (data.status === 'cancelled') {
    setStatus(`Stopped: ${series.length} clear observations from ${data.current} / ${data.total} images read${t}`);
    if (series.length) showResults();
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

function selectLocation(latlng, { pan = false } = {}) {
  $('lat').value = latlng.lat.toFixed(6);
  $('lng').value = latlng.lng.toFixed(6);
  $('lat').classList.remove('invalid');
  $('lng').classList.remove('invalid');
  showLocation(latlng);
  if (pan) map.setView(latlng, Math.max(map.getZoom(), 14));

  fetch(`/tile?lat=${latlng.lat}&lng=${latlng.lng}`)
    .then((r) => r.json())
    .then((d) => showTile(d.tile, d.outline, d.exists))
    .catch(() => {});
}

map.on('click', (e) => selectLocation(e.latlng));

// Manual coordinate entry: the "Go to" button pins the typed location.
// Pasting "lat,lng" (e.g. 52.11354,8.27282) into either field fills both.
function splitPair(input) {
  const parts = input.value.split(/[,;\s]+/).filter(Boolean);
  if (parts.length === 2) {
    $('lat').value = parts[0];
    $('lng').value = parts[1];
    return true;
  }
  return false;
}

['lat', 'lng'].forEach((id) => {
  $(id).addEventListener('input', (e) => {
    e.target.classList.remove('invalid');
    if (e.target.value.includes(',')) splitPair(e.target);
  });
});

// Validate the typed coordinates; returns an L.LatLng or null (and flags errors).
function readTypedCoords() {
  splitPair($('lat'));
  const latStr = $('lat').value.trim();
  const lngStr = $('lng').value.trim();
  const num = /^[-+]?\d+(\.\d+)?$/;
  const lat = Number(latStr);
  const lng = Number(lngStr);
  const latOk = num.test(latStr) && lat >= -90 && lat <= 90;
  const lngOk = num.test(lngStr) && lng >= -180 && lng <= 180;
  $('lat').classList.toggle('invalid', !latOk);
  $('lng').classList.toggle('invalid', !lngOk);
  if (!latOk || !lngOk) {
    setStatus('Invalid coordinates: latitude must be -90..90 and longitude -180..180 (decimal degrees)', true);
    return null;
  }
  return L.latLng(lat, lng);
}

$('coordForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const latlng = readTypedCoords();
  if (!latlng) return;
  setStatus('');
  selectLocation(latlng, { pan: true });
});

$('getData').addEventListener('click', () => {
  // If coordinates were typed but not pinned yet, pin them first.
  if ($('lat').value.trim() || $('lng').value.trim()) {
    const latlng = readTypedCoords();
    if (!latlng) return;
    if (!clickMarker || !clickMarker.getLatLng().equals(latlng, 1e-6)) {
      selectLocation(latlng, { pan: true });
    }
  }
  startJob(false);
});

// Enter anywhere on the page acts like clicking "View time series".
// A button or link reached with the keyboard (Tab) keeps its own Enter
// action; one that only has focus because it was clicked with the mouse
// (e.g. "Go to", "Copy dates") does not, so clicking around the panel and
// then pressing Enter always starts the extraction. Enter in the lat/lng
// fields pins the typed coordinates first (see the getData handler).
// (:focus-visible cannot be used for this: the Enter press itself turns it on.)
let focusFromPointer = false;
document.addEventListener('pointerdown', () => { focusFromPointer = true; }, true);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') focusFromPointer = false;
}, true);

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.repeat || e.isComposing) return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const t = e.target;
  if (t instanceof Element) {
    if (t.closest('textarea, [contenteditable=""], [contenteditable="true"], #map')) return;
    const control = t.closest('button, a[href], summary');
    if (control && control.id !== 'getData' && !focusFromPointer) return;
  }
  e.preventDefault(); // stops the coordinate form's implicit "Go to" submit
  $('getData').click();
});

// the band box is narrow: show the full name of the chosen band on hover
function syncBandTitle() {
  const sel = $('Bands');
  sel.title = sel.options[sel.selectedIndex]?.text || '';
}
$('Bands').addEventListener('change', syncBandTitle);
syncBandTitle();

// redraw the line in the chosen mode; no new extraction needed
$('lineMode').addEventListener('change', () => {
  if (!$('chartWrap').hidden) render();
});

// Stop: ask the server to cancel; the polling loop then receives the
// 'cancelled' status and draws whatever was extracted so far.
$('stopButton').addEventListener('click', async () => {
  const jobId = currentJob;
  if (!jobId) return;
  $('stopButton').disabled = true;
  setStatus('Stopping…');
  try {
    await fetch(`/cancel/${jobId}`, { method: 'POST' });
  } catch (err) {
    console.error('Error stopping job:', err);
  }
});

// export buttons: one listener each, reading the *current* data on click
function flash(button, text) {
  // keep the original content (text and icon) to restore it
  const original = button.dataset.html || button.innerHTML;
  button.dataset.html = original;
  button.textContent = text;
  button.classList.add('copied');
  setTimeout(() => {
    button.innerHTML = original;
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

// Save the graph as an image: drawn once more at (at least) 2 x resolution
// on a white background, then the chart goes back to screen resolution.
const IMAGE_SCALE = 2;

function exportBaseName() {
  const o = readOptions();
  return `forcelive_${tileName()}_${o.lat}_${o.lng}_${o.band}_${o.startDate}_${o.endDate}`;
}

function tileName() {
  const t = $('tile').textContent;
  return t && t !== '–' ? t : 'tile';
}

function saveChartImage(format, button) {
  const type = format === 'jpeg' ? 'image/jpeg' : 'image/png';
  const previous = chart.options.devicePixelRatio;
  // no tooltip or hover highlight in the image
  chart.setActiveElements([]);
  chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  chart.options.devicePixelRatio = Math.max(IMAGE_SCALE, window.devicePixelRatio || 1);
  chart.resize();
  chart.update('none');
  const out = document.createElement('canvas');
  out.width = chart.canvas.width;
  out.height = chart.canvas.height;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(chart.canvas, 0, 0);
  // back to screen resolution
  chart.options.devicePixelRatio = previous;
  chart.resize();
  chart.update('none');
  out.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${exportBaseName()}.${format === 'jpeg' ? 'jpg' : 'png'}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    flash(button, 'Saved!');
  }, type, 0.95);
}

$('savePng').addEventListener('click', (e) => saveChartImage('png', e.currentTarget));
$('saveJpeg').addEventListener('click', (e) => saveChartImage('jpeg', e.currentTarget));

$('csvButton').addEventListener('click', (e) => {
  const o = readOptions();
  const rows = ['date,days_since_epoch,sensor,file,' + o.band];
  for (const p of series) {
    rows.push(`${new Date(p.x).toISOString().slice(0, 10)},${p.d},${p.s},${p.id},${p.y}`);
  }
  const blob = new Blob([rows.join('\n') + '\n'], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `forcelive_${tileName()}_${o.lat}_${o.lng}_${o.band}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  flash(e.currentTarget, 'Saved!');
});

// overlay controls
$('visMode').addEventListener('change', () => { if (overlayPoint) showOverlay(overlayPoint); });
$('opacity').addEventListener('input', (e) => { if (overlay) overlay.setOpacity(Number(e.target.value)); });
$('clearOverlay').addEventListener('click', removeOverlay);

// Scene overlay card: greyed out with disabled controls while no scene is
// selected
function updateOverlayCard() {
  const scene = !!overlayPoint;
  $('overlayRow').classList.toggle('inactive', !scene);
  $('visMode').disabled = !scene;
  $('opacity').disabled = !scene;
}
