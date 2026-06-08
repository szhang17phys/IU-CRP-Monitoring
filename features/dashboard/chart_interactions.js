// features/dashboard/chart_interactions.js
//
// Chart interaction logic for the WLC High Bay monitoring dashboard.
// Embedded verbatim by particle_plus.py::generate_dashboard_html() immediately
// after the data-constants block (TS, COUNTS, PM, DIST, LIVE_TS, etc.).
//
// Zoom model: discrete step-based.
//   Scroll wheel / trackpad steps the dropdown through 6 fixed time windows:
//     30 min → 1 hr → 6 hr → 12 hr → 24 hr → 7 days
//   Scroll up  (deltaY < 0) = zoom in  → smaller time window (more detail).
//   Scroll down (deltaY > 0) = zoom out → larger time window (less detail).
//   Hard stops: 30 min (can't zoom in further), 7 days (can't zoom out further).
//   Each step calls filterAndRender() which resets all charts to the exact
//   selected window — no pixel-level drift, no out-of-bounds issues.

// ── Stable Y ranges — computed ONCE from the full dataset at page load ────────
// Using all data (not the current time slice) means Y never jumps when the
// dropdown changes or filterAndRender() is called again.

// Particle counts (log scale).
// Ceiling >= 8.0 keeps ISO 5–9 reference lines (3 520 – 35.2 M /m³) in view.
const _allCountVals = COUNTS.flatMap(tr => tr.y).filter(v => v !== null && v > 0);
const _rawCountMax  = _allCountVals.length ? Math.max(..._allCountVals) : 1e6;
const COUNTS_Y_RANGE = [1.5, Math.max(Math.log10(_rawCountMax) + 0.5, 8.0)];

// PM mass (linear scale).  20 % headroom; floor at 5 µg/m³.
const _allPMVals = PM.flatMap(tr => tr.y).filter(v => v !== null && v >= 0);
const _rawPMMax  = _allPMVals.length ? Math.max(..._allPMVals) : 10;
const PM_Y_MAX   = Math.max(_rawPMMax * 1.2, 5);

// Temperature (°F).  ±5 °F padding around observed range; floor at 32 °F.
const _tempVals  = TEMP_F.filter(v => v !== null && !isNaN(v));
const TEMP_Y_RANGE = _tempVals.length
  ? [Math.max(32,  Math.min(..._tempVals) - 5), Math.max(..._tempVals) + 5]
  : [60, 90];

// Relative humidity (%).  ±5 % padding; clamped to [0, 100].
const _rhVals   = RH_VALS.filter(v => v !== null && !isNaN(v));
const RH_Y_RANGE = _rhVals.length
  ? [Math.max(0,   Math.min(..._rhVals) - 5), Math.min(100, Math.max(..._rhVals) + 5)]
  : [0, 100];

// ── Layout base shared by all charts ─────────────────────────────────────────
const DARK = {
  paper_bgcolor: '#0f172a',
  plot_bgcolor:  '#D1E8E2',
  font:      { color: '#9ca3af', family: 'Courier New, monospace', size: 11 },
  margin:    { l: 60, r: 20, t: 30, b: 50 },
  hovermode: 'x unified',
  hoverlabel: { bgcolor: '#1e293b', bordercolor: '#334155', font: { size: 11 } },
  legend: { bgcolor: 'rgba(0,0,0,0)', bordercolor: '#334155', borderwidth: 1,
            font: { size: 11 }, orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0 },
  xaxis: { gridcolor: '#b2d1c9', linecolor: '#334155', zerolinecolor: '#93beb4',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
  yaxis: { gridcolor: '#b2d1c9', linecolor: '#334155', zerolinecolor: '#93beb4',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
};

// ── Plotly config shared across all charts ────────────────────────────────────
// scrollZoom: false — native Plotly scroll zoom disabled; handled by
// _attachWheelListeners() instead so scroll steps the dropdown discretely.
const PLOTLY_CFG = {
  responsive:             true,
  displaylogo:            false,
  scrollZoom:             false,
  modeBarButtonsToRemove: ['select2d', 'lasso2d'],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function sliceIdxForArray(tsArray, mins) {
  if (!mins || tsArray.length === 0) return 0;
  const cut = new Date(_parseDate(tsArray[tsArray.length - 1]).getTime() - mins * 60000);
  const i   = tsArray.findIndex(t => _parseDate(t) >= cut);
  return i < 0 ? tsArray.length - 1 : i;
}

function sliceIdx(mins) {
  return sliceIdxForArray(TS, mins);
}

function getLeftBound(divId, mins) {
  const tsArray = (divId === 'chart-env') ? LIVE_TS : TS;
  const idx = sliceIdxForArray(tsArray, mins);
  return tsArray[idx] || null;
}

function sliceTraces(traces, i) {
  return traces.map(tr => Object.assign({}, tr, {
    x: tr.x.slice(i),
    y: tr.y.slice(i),
  }));
}

function gapShapes(ts) {
  const GAP_THRESH_MS = 90 * 60 * 1000;
  const shapes = [];
  for (let k = 1; k < ts.length; k++) {
    if (_parseDate(ts[k]) - _parseDate(ts[k - 1]) > GAP_THRESH_MS) {
      shapes.push({
        type: 'rect', xref: 'x', yref: 'paper',
        x0: ts[k - 1], x1: ts[k], y0: 0, y1: 1,
        fillcolor: 'rgba(100,116,139,0.10)', line: { width: 0 }, layer: 'below',
      });
    }
  }
  return shapes;
}

function isoShapes() {
  return ISO_LINES.map(l => ({
    type: 'line', xref: 'paper', x0: 0, x1: 1,
    yref: 'y', y0: l.y, y1: l.y,
    line: { color: l.color, width: l.width, dash: l.dash },
  }));
}
function isoAnnotations() {
  return ISO_LINES.map(l => ({
    xref: 'paper', x: 1.02, yref: 'y', y: l.y,
    text: l.bold ? '<b>' + l.label + '</b>' : l.label,
    showarrow: false, xanchor: 'left',
    font: { color: l.color, size: l.bold ? 12 : 10, family: 'Courier New, monospace' },
  }));
}

function updateStats(i) {
  const ts    = TS.slice(i);
  const ch1   = COUNTS[0].y.slice(i).filter(v => v !== null && v !== undefined);
  const ch2   = COUNTS[1].y.slice(i).filter(v => v !== null && v !== undefined);
  const n     = ts.length;
  const fmt   = v => (v !== null && !isNaN(v))
    ? Math.round(v).toLocaleString() + ' /m³' : '--';
  const mean1 = ch1.length ? ch1.reduce((a, b) => a + b, 0) / ch1.length : null;
  const peak1 = ch1.length ? Math.max(...ch1) : null;
  const exc7  = ch2.filter(v => v > 352000).length;
  const exc7s = ch2.length
    ? exc7 + ' / ' + ch2.length + ' (' + (exc7 / ch2.length * 100).toFixed(0) + '%)'
    : '--';
  const gaps = gapShapes(ts).length;

  document.getElementById('stat-n').textContent     = n;
  document.getElementById('stat-mean1').textContent = fmt(mean1);
  document.getElementById('stat-peak1').textContent = fmt(peak1);

  const excEl = document.getElementById('stat-exc7');
  excEl.textContent = exc7s;
  excEl.className   = 'stat-v' + (exc7 > 0 ? ' warn' : '');

  const gapEl = document.getElementById('stat-gaps');
  gapEl.textContent = gaps > 0 ? gaps + (gaps === 1 ? ' gap' : ' gaps') : 'none';
  gapEl.className   = 'stat-v' + (gaps > 0 ? ' warn' : '');
}

// ── Main render function ──────────────────────────────────────────────────────
// Always reads the current dropdown value and renders all four charts to exactly
// that time window.  The explicit xaxis.range guarantees the viewport snaps to
// the clean selected window on every call — no drift possible.
function filterAndRender() {
  const sel  = document.getElementById('sel-range');
  const mins = parseInt(sel.value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  // maxallowed: prevents the chart from drifting into the future.
  // Explicit range: resets the viewport to the exact selected window on every render.
  const xBounds = ts.length > 0 ? { maxallowed: TS[TS.length - 1] } : {};
  const xRange  = ts.length > 0
    ? { range: [ts[0], ts[ts.length - 1]], autorange: false }
    : {};

  // ── Particle count chart (log scale) ─────────────────────────────────────
  // yaxis.fixedrange: true  →  scroll and +/- only move the X (time) axis.
  // autorange: false + COUNTS_Y_RANGE  →  Y never jumps on window changes.
  const p1 = Plotly.react('chart-counts', sliceTraces(COUNTS, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        autorange:  false,
        range:      COUNTS_Y_RANGE,
        fixedrange: true,
      }),
      xaxis:       Object.assign({}, DARK.xaxis, { title: '' }, xBounds, xRange),
      margin:      { l: 60, r: 72, t: 30, b: 50 },
      shapes:      [...gaps, ...isoShapes()],
      annotations: isoAnnotations(),
    }), PLOTLY_CFG);

  // ── PM mass chart (linear scale) ─────────────────────────────────────────
  const p2 = Plotly.react('chart-pm', sliceTraces(PM, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'μg / m³',
        rangemode:  'tozero',
        range:      [0, PM_Y_MAX],
        autorange:  false,
        fixedrange: true,
      }),
      xaxis:  Object.assign({}, DARK.xaxis, { title: '' }, xBounds, xRange),
      shapes: gaps,
    }), PLOTLY_CFG);

  // ── Size distribution bar chart ───────────────────────────────────────────
  const _distMax    = (DIST[0] && DIST[0].y.length) ? Math.max(...DIST[0].y) : 100;
  const _distLogMax = Math.log10(Math.max(_distMax, 1)) + 1.0;
  const p3 = Plotly.react('chart-dist', DIST,
    Object.assign({}, DARK, {
      showlegend: false,
      bargap:     0.3,
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        range:      [-0.5, _distLogMax],
        autorange:  false,
        fixedrange: true,
      }),
      xaxis: Object.assign({}, DARK.xaxis, {
        title:      'Particle Size (μm)',
        fixedrange: true,
      }),
    }), PLOTLY_CFG);

  // ── Environment chart (temperature + humidity) ────────────────────────────
  // TEMP_Y_RANGE / RH_Y_RANGE are pre-computed from the full dataset so neither
  // axis jumps when the time window changes.
  const livei    = sliceIdxForArray(LIVE_TS, mins);
  const envBounds = LIVE_TS.length > livei ? { maxallowed: LIVE_TS[LIVE_TS.length - 1] } : {};
  const envRange  = LIVE_TS.length > livei
    ? { range: [LIVE_TS[livei], LIVE_TS[LIVE_TS.length - 1]], autorange: false }
    : {};

  const p4 = Plotly.react('chart-env', [
    { x: LIVE_TS.slice(livei), y: TEMP_F.slice(livei), name: 'Temperature (°F)',
      type: 'scatter', mode: 'lines',
      line: { color: '#dc2626', width: 3 }, yaxis: 'y' },
    { x: LIVE_TS.slice(livei), y: RH_VALS.slice(livei), name: 'Humidity (%)',
      type: 'scatter', mode: 'lines',
      line: { color: '#1d4ed8', width: 3 }, yaxis: 'y2' },
  ], Object.assign({}, DARK, {
    margin: { l: 60, r: 70, t: 30, b: 50 },
    xaxis:  Object.assign({}, DARK.xaxis, { title: '' }, envBounds, envRange),
    yaxis:  Object.assign({}, DARK.yaxis, {
      title:      'Temperature (°F)',
      autorange:  false,
      range:      TEMP_Y_RANGE,
      fixedrange: true,
    }),
    yaxis2: {
      title:      { text: 'Humidity (%)', standoff: 15 },
      overlaying: 'y', side: 'right',
      autorange:  false,
      range:      RH_Y_RANGE,
      fixedrange: true,
      gridcolor:  '#b2d1c9', linecolor: '#334155',
      tickfont:   { color: '#6b7280', size: 10 },
      title_font: { color: '#6b7280', size: 11 },
    },
  }), PLOTLY_CFG);

  updateStats(i);

  return Promise.all([p1, p2, p3, p4]);
}

// ── Date helpers ─────────────────────────────────────────────────────────────
// Plotly relayout events return timestamps as "YYYY-MM-DD HH:MM:SS.mmm"
// (space-separated, not valid ISO 8601).  Replace the space with T so that
// new Date() parses reliably.  Numeric epoch values are handled by the else branch.
function _parseDate(str) {
  if (typeof str !== 'string') return new Date(+str);
  return new Date(str.replace(' ', 'T'));
}

// Format a JS Date as "YYYY-MM-DD HH:MM:SS" in LOCAL time, matching the
// Python-generated CSV timestamp format so Plotly reads it in the correct timezone.
function _toLocalStr(date) {
  const p = n => String(n).padStart(2, '0');
  return date.getFullYear() + '-' + p(date.getMonth() + 1) + '-' + p(date.getDate())
       + ' ' + p(date.getHours()) + ':' + p(date.getMinutes()) + ':' + p(date.getSeconds());
}

// ── Zoom state ────────────────────────────────────────────────────────────────
// _zooming is a debounce guard: while filterAndRender() is in-flight, incoming
// wheel events are ignored so rapid scrolling doesn't queue multiple renders.
let _zooming = false;

// ── Step-based zoom ───────────────────────────────────────────────────────────
// direction: -1 = zoom in  (step to smaller time window, decrease selectedIndex)
//            +1 = zoom out (step to larger time window,  increase selectedIndex)
//
// The dropdown options are ordered smallest → largest:
//   index 0: 30 min  (hard stop — can't zoom in further)
//   index 1: 1 hr
//   index 2: 6 hr
//   index 3: 12 hr
//   index 4: 24 hr
//   index 5: 7 days  (hard stop — can't zoom out further)
//
// Stepping outside [0, options.length-1] is silently ignored.
function _stepZoom(direction) {
  if (_zooming) return;
  const sel      = document.getElementById('sel-range');
  const newIndex = sel.selectedIndex + direction;
  // Hard stops: clamp to valid range and do nothing if already at the limit.
  if (newIndex < 0 || newIndex >= sel.options.length) return;
  sel.selectedIndex = newIndex;
  _zooming = true;
  filterAndRender().then(function () {
    _zooming = false;
  }).catch(function () {
    _zooming = false;
  });
}

// ── Attach wheel listeners ────────────────────────────────────────────────────
// Intercepts scroll-wheel / trackpad events on all three time-series chart divs
// and converts them into discrete zoom steps via _stepZoom().
//
// Convention (matches Plotly's original scroll direction):
//   deltaY < 0  (scroll up / pinch out)  → zoom in  → _stepZoom(-1)
//   deltaY > 0  (scroll down / pinch in) → zoom out → _stepZoom(+1)
//
// { passive: false } is required so that ev.preventDefault() can suppress the
// browser's default page-scroll behaviour while the cursor is over a chart.
window._attachWheelListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    document.getElementById(divId).addEventListener('wheel', function (ev) {
      ev.preventDefault();
      // deltaY > 0: scroll down = zoom out (+1); deltaY < 0: scroll up = zoom in (-1)
      _stepZoom(ev.deltaY > 0 ? +1 : -1);
    }, { passive: false });
  });
};

// ── Attach modebar +/- button listeners ──────────────────────────────────────
// Plotly's zoom-in (+) and zoom-out (-) modebar buttons fire plotly_relayout
// with a new xaxis.range.  We intercept that event, detect which button was
// pressed by comparing the new span to the current dropdown's span, and
// delegate to _stepZoom() so the behaviour is identical to the scroll wheel.
//
//   New span < current span → zoom-in  (+) pressed → _stepZoom(-1)
//   New span > current span → zoom-out (-) pressed → _stepZoom(+1)
//   New span ≈ current span (pan) → ignored
//
// The _zooming guard (set by _stepZoom / filterAndRender) prevents the
// relayout event that filterAndRender itself fires from triggering a
// second recursive call.
window._attachRelayoutListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    document.getElementById(divId).on('plotly_relayout', function (ev) {
      if (_zooming) return;  // ignore redraws triggered by our own filterAndRender

      // Extract the new xaxis range from the event (Plotly uses two formats).
      var x0 = ev['xaxis.range[0]'];
      var x1 = ev['xaxis.range[1]'];
      if (x0 === undefined && Array.isArray(ev['xaxis.range'])) {
        x0 = ev['xaxis.range'][0];
        x1 = ev['xaxis.range'][1];
      }
      if (x0 === undefined || x1 === undefined) return;  // not a range event

      var newSpanMs     = _parseDate(x1).getTime() - _parseDate(x0).getTime();
      var currentMins   = parseInt(document.getElementById('sel-range').value);
      var currentSpanMs = currentMins * 60 * 1000;

      if (newSpanMs < currentSpanMs - 1000) {
        _stepZoom(-1);   // zoom-in (+) button
      } else if (newSpanMs > currentSpanMs + 1000) {
        _stepZoom(+1);   // zoom-out (-) button
      }
      // If spans are equal (pan), do nothing — chart stays at current window.
    });
  });
};

// ── Initial render ────────────────────────────────────────────────────────────
filterAndRender();
// Attach wheel listeners after charts exist in the DOM.
window._attachWheelListeners();
// Attach modebar +/- listeners after charts exist in the DOM.
window._attachRelayoutListeners();

// ── Close notification dropdown on outside click ──────────────────────────────
document.addEventListener('click', function (e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});
