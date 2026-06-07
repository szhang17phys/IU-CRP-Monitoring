// features/dashboard/chart_interactions.js
//
// Chart interaction logic for the WLC High Bay monitoring dashboard.
// Embedded verbatim by particle_plus.py::generate_dashboard_html() immediately
// after the data-constants block (TS, COUNTS, PM, DIST, LIVE_TS, etc.).
//
// Fixes in this file:
//   1. All particle & env Y axes are stable — pre-computed from the full
//      dataset so they never jump when the time-range dropdown or zoom changes.
//   2. fixedrange: true on every Y axis → +/- and scroll-zoom only affect X.
//   3. scrollZoom: true so trackpad / mouse-wheel zooms the time axis.
//   4. Zoom-out expansion: when the user zooms/scrolls past the left edge of
//      the current time window, the dropdown steps up automatically and the
//      chart reloads with more data.  Two intermediate steps (2 days, 3 days)
//      are inserted into the dropdown so expansion is gradual.

// ── Ensure dropdown has intermediate expansion steps ──────────────────────────
// Adds "Last 2 days" and "Last 3 days" between the existing 24 hr and 7 days
// options so zoom-out expansion is gradual.  Done in JS so particle_plus.py
// does not need to change.
(function () {
  const sel = document.getElementById('sel-range');
  const existing = new Set(Array.from(sel.options).map(o => parseInt(o.value)));
  const extra = [
    { value: 2880,  label: 'Last 2 days' },
    { value: 4320,  label: 'Last 3 days' },
  ];
  extra.forEach(({ value, label }) => {
    if (existing.has(value)) return;
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    // Insert in ascending order
    let before = null;
    for (let i = 0; i < sel.options.length; i++) {
      if (parseInt(sel.options[i].value) > value) { before = sel.options[i]; break; }
    }
    sel.add(opt, before);
  });
})();

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
  plot_bgcolor:  '#0f172a',
  font:      { color: '#9ca3af', family: 'Courier New, monospace', size: 11 },
  margin:    { l: 60, r: 20, t: 30, b: 50 },
  hovermode: 'x unified',
  hoverlabel: { bgcolor: '#1e293b', bordercolor: '#334155', font: { size: 11 } },
  legend: { bgcolor: 'rgba(0,0,0,0)', bordercolor: '#334155', borderwidth: 1,
            font: { size: 11 }, orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0 },
  xaxis: { gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
  yaxis: { gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: { color: '#6b7280', size: 10 },
           title_font: { color: '#6b7280', size: 11 } },
};

// ── Plotly config shared across all charts ────────────────────────────────────
const PLOTLY_CFG = {
  responsive:             true,
  displaylogo:            false,
  scrollZoom:             true,
  modeBarButtonsToRemove: ['select2d', 'lasso2d'],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function sliceIdx(mins) {
  if (!mins || TS.length === 0) return 0;
  const cut = new Date(new Date(TS[TS.length - 1]) - mins * 60000);
  const i   = TS.findIndex(t => new Date(t) >= cut);
  return i < 0 ? TS.length - 1 : i;
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
    if (new Date(ts[k]) - new Date(ts[k - 1]) > GAP_THRESH_MS) {
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
function filterAndRender() {
  const sel  = document.getElementById('sel-range');
  const mins = parseInt(sel.value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  // When the dropdown is at its maximum option (Last 7 days), set xaxis.minallowed
  // to the first data point so Plotly physically prevents zooming/panning further
  // left.  Below max, leave minallowed unset so the expansion listener can fire.
  const isAtMax  = sel.selectedIndex >= sel.options.length - 1;
  const xBounds  = (isAtMax && ts.length > 0) ? { minallowed: ts[0] } : {};

  // ── Particle count chart (log scale) ─────────────────────────────────────
  // yaxis.fixedrange: true  →  +/- and scroll-zoom only move the X (time) axis.
  // autorange: false + COUNTS_Y_RANGE  →  Y never jumps on dropdown changes.
  Plotly.react('chart-counts', sliceTraces(COUNTS, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'Counts / m³',
        type:       'log',
        autorange:  false,
        range:      COUNTS_Y_RANGE,
        fixedrange: true,
      }),
      xaxis:       Object.assign({}, DARK.xaxis, { title: '' }, xBounds),
      margin:      { l: 60, r: 72, t: 30, b: 50 },
      shapes:      [...gaps, ...isoShapes()],
      annotations: isoAnnotations(),
    }), PLOTLY_CFG);

  // ── PM mass chart (linear scale) ─────────────────────────────────────────
  Plotly.react('chart-pm', sliceTraces(PM, i),
    Object.assign({}, DARK, {
      yaxis: Object.assign({}, DARK.yaxis, {
        title:      'μg / m³',
        rangemode:  'tozero',
        range:      [0, PM_Y_MAX],
        autorange:  false,
        fixedrange: true,
      }),
      xaxis:  Object.assign({}, DARK.xaxis, { title: '' }, xBounds),
      shapes: gaps,
    }), PLOTLY_CFG);

  // ── Size distribution bar chart ───────────────────────────────────────────
  const _distMax    = (DIST[0] && DIST[0].y.length) ? Math.max(...DIST[0].y) : 100;
  const _distLogMax = Math.log10(Math.max(_distMax, 1)) + 0.3;
  Plotly.react('chart-dist', DIST,
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
  // TEMP_Y_RANGE / RH_Y_RANGE are computed from the full dataset at page load,
  // so neither axis jumps when the time window or zoom changes.
  // fixedrange: true on both Y axes — +/- and scroll only move the X axis,
  // keeping the temp and RH scales stable for comparison across sessions.
  const livei = (LIVE_TS.length === 0 || !mins) ? 0 : (() => {
    const cut = new Date(new Date(LIVE_TS[LIVE_TS.length - 1]) - mins * 60000);
    const j   = LIVE_TS.findIndex(t => new Date(t) >= cut);
    return j < 0 ? LIVE_TS.length - 1 : j;
  })();
  // Env chart uses its own data array (LIVE_TS), so compute its own minallowed.
  const envXBounds = (isAtMax && LIVE_TS.length > livei)
    ? { minallowed: LIVE_TS[livei] } : {};

  Plotly.react('chart-env', [
    { x: LIVE_TS.slice(livei), y: TEMP_F.slice(livei), name: 'Temperature (°F)',
      type: 'scatter', mode: 'lines',
      line: { color: '#ff6b6b', width: 2 }, yaxis: 'y' },
    { x: LIVE_TS.slice(livei), y: RH_VALS.slice(livei), name: 'Humidity (%)',
      type: 'scatter', mode: 'lines',
      line: { color: '#4ecdc4', width: 2 }, yaxis: 'y2' },
  ], Object.assign({}, DARK, {
    margin: { l: 60, r: 70, t: 30, b: 50 },
    xaxis:  Object.assign({}, DARK.xaxis, { title: '' }, envXBounds),
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
      gridcolor:  '#1e293b', linecolor: '#334155',
      tickfont:   { color: '#6b7280', size: 10 },
      title_font: { color: '#6b7280', size: 11 },
    },
  }), PLOTLY_CFG);

  updateStats(i);
}

// ── Zoom-out expansion ────────────────────────────────────────────────────────
// When the user zooms or scrolls out past the left edge of the current time
// window (X axis start moves before the earliest loaded data point), the
// dropdown automatically steps to the next larger option and filterAndRender()
// reloads the chart with more data.
//
// Guard flag prevents re-entrant calls in case Plotly.react() inside
// filterAndRender() emits a spurious relayout event.
(function () {
  let _expanding = false;

  function tryExpandTimeRange(xRangeStart) {
    if (_expanding) return;
    if (!TS.length) return;

    const sel      = document.getElementById('sel-range');
    const mins     = parseInt(sel.value);
    const dataEnd  = new Date(TS[TS.length - 1]);
    const dataStart = new Date(dataEnd.getTime() - mins * 60 * 1000);

    // Only expand when the panned/zoomed X start is before the data window start
    if (new Date(xRangeStart) < dataStart && sel.selectedIndex < sel.options.length - 1) {
      _expanding = true;
      sel.selectedIndex++;
      filterAndRender();
      _expanding = false;
    }
  }

  // Attach AFTER the initial filterAndRender() so the Plotly div exists.
  // Plotly.react() preserves event listeners on the same div element, so
  // this only needs to run once.
  function attachListener() {
    document.getElementById('chart-counts').on('plotly_relayout', function (ev) {
      // Only care about explicit X range changes, not autorange resets.
      const x0 = ev['xaxis.range[0]'];
      if (x0 !== undefined) tryExpandTimeRange(x0);
    });
  }

  // Exposed so the initial-render block below can call it.
  window._attachZoomExpansion = attachListener;
})();

// ── Initial render ────────────────────────────────────────────────────────────
filterAndRender();
// Attach zoom-expansion listener now that chart-counts div is initialised.
window._attachZoomExpansion();

// ── Close notification dropdown on outside click ──────────────────────────────
document.addEventListener('click', function (e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});
