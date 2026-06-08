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

// (Dynamic dropdown insertion removed, options now defined in particle_plus.py HTML template)

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
function filterAndRender() {
  const sel  = document.getElementById('sel-range');
  const mins = parseInt(sel.value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  // X axis hard bounds for particle charts:
  //   maxallowed = latest data point — always set so zooming/panning cannot
  //                drift into the future (prevents the chart reaching 2050+).
  //   minallowed = first data point in the current slice — only set at the
  //                maximum dropdown option (7 days) to stop left-expansion;
  //                left unset below max so the zoom-out expansion listener fires.
  const isAtMax = sel.selectedIndex >= sel.options.length - 1;
  const xBounds = ts.length > 0 ? Object.assign(
    { maxallowed: ts[ts.length - 1] },
    isAtMax ? { minallowed: ts[0] } : {}
  ) : {};

  // ── Particle count chart (log scale) ─────────────────────────────────────
  // yaxis.fixedrange: true  →  +/- and scroll-zoom only move the X (time) axis.
  // autorange: false + COUNTS_Y_RANGE  →  Y never jumps on dropdown changes.
  const p1 = Plotly.react('chart-counts', sliceTraces(COUNTS, i),
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
  const p2 = Plotly.react('chart-pm', sliceTraces(PM, i),
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
  // TEMP_Y_RANGE / RH_Y_RANGE are computed from the full dataset at page load,
  // so neither axis jumps when the time window or zoom changes.
  // fixedrange: true on both Y axes — +/- and scroll only move the X axis,
  // keeping the temp and RH scales stable for comparison across sessions.
  const livei = sliceIdxForArray(LIVE_TS, mins);
  // Env chart uses LIVE_TS — same logic: maxallowed always set, minallowed only at max.
  const envXBounds = LIVE_TS.length > livei ? Object.assign(
    { maxallowed: LIVE_TS[LIVE_TS.length - 1] },
    isAtMax ? { minallowed: LIVE_TS[livei] } : {}
  ) : {};

  const p4 = Plotly.react('chart-env', [
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

  // Update prevRange values at render/range change
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    const tsArray = (divId === 'chart-env') ? LIVE_TS : TS;
    if (tsArray.length) {
      const idx = sliceIdxForArray(tsArray, mins);
      _updatePrevRange(divId, _parseDate(tsArray[idx]).getTime(), _parseDate(tsArray[tsArray.length - 1]).getTime());
    }
  });

  return Promise.all([p1, p2, p3, p4]);
}

// ── Zoom behaviour: expansion, left/right hard stops, zoom-in limit ───────────
//
// Rules enforced on every time-series chart (counts, PM, env):
//
//   Zoom-out expansion   When X left edge crosses the data-window start and
//                        we are not yet at the 7-day max, step the dropdown
//                        up and reload all charts.
//
//   Left hard stop       At 7-day max, snap x0 back to TS[sliceIdx(mins)]
//                        (first timestamp in the current window), computed
//                        fresh from the data at event time — no stored state.
//
//   Right hard stop      maxallowed in the layout (always the latest sample).
//
//   Zoom-in limit        Visible span < 1 min → snap back to 1 min centred.
//
// All corrections call Plotly.relayout() synchronously (no requestAnimationFrame).
// The _zooming flag blocks the recursive plotly_relayout that Plotly emits
// during the correction, making the synchronous call safe.
// rAF was removed because it introduced a race: the user's scroll wheel fires
// many events before the deferred frame fires, sending the axis to sub-second
// before the correction runs.

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

// ── Shared constants / state ──────────────────────────────────────────────────
const MIN_SPAN_MS = 10 * 60 * 1000;   // 10 minutes hard floor for zoom-in (approx 12 steps from 24h)

let _zooming = false;   // re-entrancy guard — blocks recursive plotly_relayout

// Track visible ranges of the charts to detect zoom-in and anchor appropriately.
const _prevRange = { 'chart-counts': null, 'chart-pm': null, 'chart-env': null };

function _updatePrevRange(divId, x0, x1) {
  _prevRange[divId] = { x0: x0, x1: x1 };
}

// Enforce minimum zoom-in floor, max allowed end date, and zoom-out bounds
function _enforceZoomConstraints(divId, targetX0, targetX1, x0orig, x1orig) {
  const tsArray = (divId === 'chart-env') ? LIVE_TS : TS;
  if (!tsArray.length) return;

  const dataEndMs = _parseDate(tsArray[tsArray.length - 1]).getTime();
  const dataStartMs = _parseDate(tsArray[0]).getTime();
  let x0 = targetX0;
  let x1 = targetX1;
  let span = x1 - x0;

  // 1. Zoom-in clamp (floor)
  if (span < MIN_SPAN_MS) {
    console.log(`[Zoom] ${divId} zoom-in limit hit! Clamping to floor.`);
    span = Math.min(MIN_SPAN_MS, dataEndMs - dataStartMs);
    x1 = targetX1;
    x0 = x1 - span;
    if (x0 < dataStartMs) {
      x0 = dataStartMs;
      x1 = x0 + span;
    }
  }

  // 2. Right hard stop (never zoom past latest data)
  if (x1 > dataEndMs) {
    console.log(`[Zoom] ${divId} right hard stop hit! Snapping to latest data.`);
    x0 -= (x1 - dataEndMs);
    x1 = dataEndMs;
  }

  // 3. Zoom-out expansion / left hard stop
  const sel = document.getElementById('sel-range');
  const mins = parseInt(sel.value);
  const minAllowedTime = Math.max(dataEndMs - mins * 60 * 1000, dataStartMs);

  if (x0 < minAllowedTime) {
    if (sel.selectedIndex < sel.options.length - 1) {
      // Below max: step the dropdown up and reload
      console.log(`[Zoom] ${divId} left edge crossed current window! Expanding dropdown from ${mins} mins.`);
      _zooming = true;
      sel.selectedIndex++;
      filterAndRender().then(function () {
        _zooming = false;
      }).catch(function () {
        _zooming = false;
      });
      return;
    } else {
      // At max (7-day): snap back to left boundary
      console.log(`[Zoom] ${divId} zoom-out limit hit! Snapping to oldest allowed data.`);
      x0 = minAllowedTime;
      x1 = Math.min(x0 + span, dataEndMs);
    }
  }

  // If computed range differs from the original layout range, perform Plotly relayout.
  if (Math.abs(x0 - x0orig) > 1000 || Math.abs(x1 - x1orig) > 1000) {
    console.log(`[Zoom] ${divId} performing relayout: [${_toLocalStr(new Date(x0))} to ${_toLocalStr(new Date(x1))}]`);
    _zooming = true;
    Plotly.relayout(divId, {
      'xaxis.range[0]': _toLocalStr(new Date(x0)),
      'xaxis.range[1]': _toLocalStr(new Date(x1))
    }).then(function () {
      _zooming = false;
      _updatePrevRange(divId, x0, x1);
    }).catch(function () {
      _zooming = false;
    });
  } else {
    _updatePrevRange(divId, x0, x1);
  }
}

// ── Attach listeners ──────────────────────────────────────────────────────────
window._attachZoomListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    document.getElementById(divId).on('plotly_relayout', function (ev) {
      if (_zooming) return;

      // Handle both Plotly event formats for X range:
      let x0 = ev['xaxis.range[0]'];
      let x1 = ev['xaxis.range[1]'];
      if (x0 === undefined && Array.isArray(ev['xaxis.range'])) {
        x0 = ev['xaxis.range'][0];
        x1 = ev['xaxis.range'][1];
      }
      if (x0 === undefined) return;

      const x0ms = _parseDate(x0).getTime();
      const x1ms = _parseDate(x1).getTime();
      if (isNaN(x0ms) || isNaN(x1ms)) return;

      const newSpan = x1ms - x0ms;
      const prev = _prevRange[divId];
      let isZoomIn = false;
      if (prev) {
        const prevSpan = prev.x1 - prev.x0;
        if (newSpan < prevSpan - 1000) {
          isZoomIn = true;
        }
      }

      let targetX0 = x0ms;
      let targetX1 = x1ms;

      const tsArray = (divId === 'chart-env') ? LIVE_TS : TS;
      if (tsArray.length && isZoomIn && prev) {
        const dataEndMs = _parseDate(tsArray[tsArray.length - 1]).getTime();
        // Prefer towards the right side (towards the latest data) when zooming in
        let rightEdge = prev.x1;
        if (dataEndMs - rightEdge < 5 * 60 * 1000) {
          rightEdge = dataEndMs;
        }
        targetX1 = rightEdge;
        targetX0 = rightEdge - newSpan;
      }

      _enforceZoomConstraints(divId, targetX0, targetX1, x0ms, x1ms);
    });
  });
};

// ── Initial render ────────────────────────────────────────────────────────────
filterAndRender();
// Attach after initial render so all three chart divs exist.
window._attachZoomListeners();

// ── Close notification dropdown on outside click ──────────────────────────────
document.addEventListener('click', function (e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});
