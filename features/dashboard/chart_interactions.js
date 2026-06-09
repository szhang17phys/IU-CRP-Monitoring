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

  // Aggregate the dense ~10 s env data into 5-minute means, then show each bin
  // as a scatter marker with measurement-uncertainty error bars:
  //   temperature ±0.36 °F  (= ±0.2 °C instrument spec, converted to °F)
  //   humidity    ±1 %
  const ENV_BIN_MS = 5 * 60 * 1000;   // 5-minute aggregation buckets
  const tempBin = binByTime(LIVE_TS.slice(livei), TEMP_F.slice(livei),  ENV_BIN_MS);
  const rhBin   = binByTime(LIVE_TS.slice(livei), RH_VALS.slice(livei), ENV_BIN_MS);

  const p4 = Plotly.react('chart-env', [
    { x: tempBin.x, y: tempBin.y, name: 'Temperature (°F)',
      type: 'scatter', mode: 'markers',
      marker: { color: '#dc2626', size: 5 },
      error_y: { type: 'constant', value: 0.36, color: 'rgba(220,38,38,0.7)',
                 thickness: 1, width: 2 },
      yaxis: 'y' },
    { x: rhBin.x, y: rhBin.y, name: 'Humidity (%)',
      type: 'scatter', mode: 'markers',
      marker: { color: '#1d4ed8', size: 5 },
      error_y: { type: 'constant', value: 1, color: 'rgba(29,78,216,0.7)',
                 thickness: 1, width: 2 },
      yaxis: 'y2' },
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

// ── Time-bin aggregation ──────────────────────────────────────────────────────
// The env data is sampled ~every 10 s, far too dense to show one error bar per
// point. binByTime() groups parallel (timestamp, value) arrays into fixed-width
// time buckets and returns the mean value per bucket, plotted at the mean
// timestamp of the points in that bucket (so a marker never lands past the
// latest sample). Empty/NaN values and empty buckets are skipped.
function binByTime(tsArr, valArr, binMs) {
  const buckets = new Map();   // bucketStartMs -> { sumV, sumT, n }
  for (let k = 0; k < tsArr.length; k++) {
    const v = valArr[k];
    if (v === null || v === undefined || isNaN(v)) continue;
    const tms = _parseDate(tsArr[k]).getTime();
    if (isNaN(tms)) continue;
    const key = Math.floor(tms / binMs) * binMs;
    let b = buckets.get(key);
    if (!b) { b = { sumV: 0, sumT: 0, n: 0 }; buckets.set(key, b); }
    b.sumV += v; b.sumT += tms; b.n += 1;
  }
  const keys = Array.from(buckets.keys()).sort((a, b) => a - b);
  const x = [], y = [];
  for (const key of keys) {
    const b = buckets.get(key);
    x.push(_toLocalStr(new Date(b.sumT / b.n)));   // mean time within the bucket
    y.push(b.sumV / b.n);                            // mean value within the bucket
  }
  return { x: x, y: y };
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
  let newIndex = sel.selectedIndex + direction;
  
  // Hard stops: clamp to valid range.
  // We MUST call filterAndRender() even if already at the limit, because if this
  // was triggered by a modebar +/- click, Plotly natively zoomed the chart and
  // we need filterAndRender() to snap it back to our hard limit.
  if (newIndex < 0) newIndex = 0;
  if (newIndex >= sel.options.length) newIndex = sel.options.length - 1;
  
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
      }
      // Zoom-out (-) is handled by _attachZoomOutButtonListeners() via a direct
      // click handler — span inference is unreliable for zoom-out because the
      // maxallowed right-edge clamp and the nominal-vs-actual window mismatch
      // keep the reported span from growing past currentSpanMs.
      // If spans are equal (pan), do nothing — chart stays at current window.
    });
  });
};

// ── Attach modebar zoom-out (-) button listener ───────────────────────────────
// Bind directly to Plotly's "Zoom out" modebar button (event delegation on each
// chart div, so it survives modebar re-creation) and step to the next-larger
// time window — identical behaviour to scroll-out and the '-' keyboard shortcut.
//
// Capture phase + stopPropagation: the modebar button is a NATIVE Plotly button,
// so a click would otherwise ALSO run Plotly's built-in zoom-out, which fires a
// plotly_relayout that races our _stepZoom(+1) through the shared _zooming guard
// (intermittently swallowing the step). Intercepting in the capture phase lets us
// cancel the native handler before it runs, so only our discrete step happens.
window._attachZoomOutButtonListeners = function () {
  ['chart-counts', 'chart-pm', 'chart-env'].forEach(function (divId) {
    document.getElementById(divId).addEventListener('click', function (ev) {
      if (ev.target.closest('[data-attr="zoom"][data-val="out"]')) {
        ev.stopPropagation();   // block Plotly's native zoom-out handler
        ev.preventDefault();
        _stepZoom(+1);          // zoom-out (-) button → next-larger time window
      }
    }, true);   // true = capture phase, runs before the button's own handler
  });
};

// ── Auto-refresh + manual refresh control ─────────────────────────────────────
// The dashboard data is baked into index.html (regenerated by the daemon), so
// "refresh to latest" means re-fetching the page. To keep that smooth:
//   • the selected time range is preserved across reloads (sessionStorage) so a
//     refresh — auto or manual — never throws away the user's current zoom,
//   • the page auto-reloads every 60 s (skipping a cycle while the Alerts panel
//     is open, so it isn't yanked away mid-read),
//   • a manual refresh button is injected into the header (next to Time Range).
//     Kept entirely here so particle_plus.py is untouched.
var AUTO_REFRESH_MS = 60000;
var _refreshTimer   = null;

function _restoreTimeRange() {
  try {
    var saved = sessionStorage.getItem('wlc-range');
    if (saved === null) return;
    var sel = document.getElementById('sel-range');
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === saved) { sel.selectedIndex = i; break; }
    }
  } catch (e) { /* sessionStorage unavailable → keep the default range */ }
}

function _saveTimeRange() {
  try {
    sessionStorage.setItem('wlc-range', document.getElementById('sel-range').value);
  } catch (e) { /* ignore */ }
}

function _refreshNow() {
  var btn = document.getElementById('wlc-refresh-btn');
  if (btn) btn.classList.add('spinning');   // brief spin so the reload feels intentional
  _saveTimeRange();
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  setTimeout(function () { window.location.reload(); }, 450);
}

function _autoRefreshTick() {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open')) return;  // don't close the panel mid-read
  _refreshNow();
}

function _attachRefreshControl() {
  if (document.getElementById('wlc-refresh-style')) return;  // idempotent

  var css = document.createElement('style');
  css.id = 'wlc-refresh-style';
  css.textContent =
    '@keyframes wlc-spin{to{transform:rotate(360deg)}}' +
    '.wlc-refresh-wrap{display:flex;align-items:center;align-self:flex-end;margin-bottom:0}' +
    // White square so the control stands out against the dark header; sized to
    // match the Time Range dropdown's height.
    '.wlc-refresh-btn{display:inline-flex;align-items:center;justify-content:center;' +
      'width:24px;height:24px;background:#f1f5f9;border:1px solid #cbd5e1;border-radius:5px;' +
      'color:#0f172a;cursor:pointer;padding:0;' +
      'transition:border-color .15s,color .15s,transform .15s,box-shadow .15s,background .15s}' +
    '.wlc-refresh-btn:hover{background:#ffffff;border-color:#38bdf8;color:#0b1220;' +
      'transform:rotate(-40deg);box-shadow:0 0 0 1px rgba(56,189,248,.35),0 0 12px rgba(56,189,248,.25)}' +
    '.wlc-refresh-btn:active{transform:scale(.92)}' +
    '.wlc-refresh-btn svg{width:14px;height:14px;display:block}' +
    '.wlc-refresh-btn.spinning svg{animation:wlc-spin .6s linear infinite}' +
    // Larger, more readable "Last pushed" label (overrides the .updated rule
    // from particle_plus.py, which is left untouched).
    '.updated{font-size:14px}';
  document.head.appendChild(css);

  var wrap = document.createElement('div');
  wrap.className = 'wlc-refresh-wrap';
  wrap.innerHTML =
    '<button id="wlc-refresh-btn" class="wlc-refresh-btn" type="button" ' +
      'title="Refresh to latest data" aria-label="Refresh to latest data">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>' +
    '</button>';

  var updated = document.querySelector('.updated');
  if (updated && updated.parentNode) {
    updated.parentNode.insertBefore(wrap, updated);  // before "Last pushed" → next to Time Range
  } else {
    var controls = document.querySelector('.controls');
    if (!controls) return;
    controls.appendChild(wrap);
  }

  var btn = document.getElementById('wlc-refresh-btn');
  btn.addEventListener('click', _refreshNow);

  // Size the square to the Time Range dropdown's ACTUAL rendered height in this
  // browser — native <select> heights vary by OS/browser, so measuring live is
  // the only reliable way to make the square's side equal the rectangle's height.
  var selBox = document.getElementById('sel-range');
  if (selBox) {
    var h = Math.round(selBox.getBoundingClientRect().height);
    if (h >= 16 && h <= 60) {            // sanity-bound the measurement
      btn.style.width  = h + 'px';
      btn.style.height = h + 'px';
      var svg = btn.querySelector('svg');
      if (svg) {
        var icon = Math.round(h * 0.55);  // keep the glyph proportional
        svg.style.width  = icon + 'px';
        svg.style.height = icon + 'px';
      }
    }
  }
}

// ── Initial render ────────────────────────────────────────────────────────────
_restoreTimeRange();   // keep the user's zoom level across reloads
filterAndRender();
// Attach wheel listeners after charts exist in the DOM.
window._attachWheelListeners();
// Attach modebar +/- listeners after charts exist in the DOM.
window._attachRelayoutListeners();
// Attach the direct zoom-out (-) modebar button handler.
window._attachZoomOutButtonListeners();
// Inject the refresh control and start the 60 s auto-refresh.
_attachRefreshControl();
window.addEventListener('beforeunload', _saveTimeRange);
_refreshTimer = setInterval(_autoRefreshTick, AUTO_REFRESH_MS);

document.addEventListener('click', function (e) {
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {
    drop.classList.remove('open');
  }
});

// ── Attach keyboard shortcuts (+/-) ───────────────────────────────────────────
window.addEventListener('keydown', function(ev) {
  // Ignore if user is typing in an input/textarea
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA') return;

  // '-' or '_' zooms out (+1)
  if (ev.key === '-' || ev.key === '_') {
    _stepZoom(+1);
  }
  // '+' or '=' zooms in (-1)
  else if (ev.key === '+' || ev.key === '=') {
    _stepZoom(-1);
  }
});
