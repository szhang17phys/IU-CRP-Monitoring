#!/usr/bin/env python3
"""
WLC High Bay - Notification Center badge component
===================================================
Returns CSS, HTML, and JS for a clickable status badge that looks identical
to the ISO 14644-1 class badge.  Clicking it opens/closes a dropdown showing
current sensor status and recent email alerts.

Public API
----------
    component = build_notification_component(latest_rec, alert_state_path)
    component['css']        -- CSS rules to inject into the dashboard <style> block
    component['badge_html'] -- HTML for the badge + hidden dropdown
    component['js']         -- JS snippet to inject before </script>
"""

import json
import os
from datetime import datetime, timedelta

# ── Thresholds (mirror features/alerts/alerts.py defaults) ────────────────────
_RH_LOW  = 20.0       # %
_RH_HIGH = 90.0       # %
_TF_LOW  = 33.0       # degF
_TF_HIGH = 120.0      # degF
_P_HIGH  = 100_000    # counts/m3 at 0.3 um


def _safe_float(val):
    try:
        return float(val) if val not in (None, '', 'None') else None
    except (ValueError, TypeError):
        return None


def _get_real_ts(rec):
    """Return a timestamp string from a CSV record dict, or None."""
    if not rec:
        return None
    d = (rec.get('date') or '').strip()
    t = (rec.get('time') or '').strip()
    if d and t:
        return f'{d} {t}'
    return (rec.get('sync_time') or '').strip() or None


def build_notification_component(latest_rec, alert_state_path):
    """
    Parameters
    ----------
    latest_rec       : dict | None  -- most recent CSV row as a dict
    alert_state_path : str          -- path to alert_state.json from alerts.py

    Returns
    -------
    dict with keys 'css', 'badge_html', 'js'
    """

    # ── Load alert state ──────────────────────────────────────────────────────
    alert_state = {}
    if alert_state_path and os.path.exists(alert_state_path):
        try:
            with open(alert_state_path) as f:
                alert_state = json.load(f)
        except Exception:
            pass

    # ── Build status rows ─────────────────────────────────────────────────────
    # Each row: (status, message)  status in {ok, warn, alert, email, info, mute}
    rows = []

    # 1. Last sample time
    last_ts = _get_real_ts(latest_rec)
    if last_ts:
        try:
            last_dt   = datetime.fromisoformat(last_ts.replace(' ', 'T'))
            ago_s     = int((datetime.now() - last_dt).total_seconds())
            if ago_s < 3600:
                ago_label = f'{ago_s // 60}\u00a0min\u00a0ago'
            elif ago_s < 86400:
                ago_label = f'{ago_s // 3600}\u00a0hr\u00a0ago'
            else:
                ago_label = f'{ago_s // 86400}\u00a0day(s)\u00a0ago'
            rows.append(('info',
                f'\u25cf\u00a0Last\u00a0sample:\u00a0{last_ts}\u00a0({ago_label})'))
        except Exception:
            rows.append(('info', f'\u25cf\u00a0Last\u00a0sample:\u00a0{last_ts}'))
    else:
        rows.append(('warn', '\u25cf\u00a0Last\u00a0sample:\u00a0unknown'))

    # 2. Relative humidity
    rh = _safe_float(latest_rec.get('RH_pct')) if latest_rec else None
    if rh is not None:
        if rh < _RH_LOW:
            rows.append(('alert',
                f'\u25b2\u00a0RH\u00a0{rh:.0f}%\u00a0\u2014\u00a0below\u00a0{_RH_LOW:.0f}%'
                '\u00a0(static\u00a0risk)'))
        elif rh > _RH_HIGH:
            rows.append(('alert',
                f'\u25b2\u00a0RH\u00a0{rh:.0f}%\u00a0\u2014\u00a0above\u00a0{_RH_HIGH:.0f}%'
                '\u00a0(condensation\u00a0risk)'))
        else:
            rows.append(('ok', f'\u25cf\u00a0RH\u00a0{rh:.0f}%\u00a0\u2014\u00a0nominal'))
    else:
        rows.append(('mute', '\u25cb\u00a0RH:\u00a0no\u00a0sensor\u00a0data'))

    # 3. Temperature
    tc = _safe_float(latest_rec.get('temp_C')) if latest_rec else None
    if tc is not None and tc > 0:
        tf = round(tc * 9 / 5 + 32, 1)
        if tf < _TF_LOW:
            rows.append(('alert',
                f'\u25b2\u00a0Temp\u00a0{tc:.1f}\u00b0C\u00a0/\u00a0{tf:.0f}\u00b0F'
                f'\u00a0\u2014\u00a0below\u00a0{_TF_LOW:.0f}\u00b0F'))
        elif tf > _TF_HIGH:
            rows.append(('alert',
                f'\u25b2\u00a0Temp\u00a0{tc:.1f}\u00b0C\u00a0/\u00a0{tf:.0f}\u00b0F'
                f'\u00a0\u2014\u00a0above\u00a0{_TF_HIGH:.0f}\u00b0F'))
        else:
            rows.append(('ok',
                f'\u25cf\u00a0Temp\u00a0{tc:.1f}\u00b0C\u00a0/\u00a0{tf:.0f}\u00b0F'
                '\u00a0\u2014\u00a0nominal'))
    else:
        rows.append(('mute', '\u25cb\u00a0Temp:\u00a0no\u00a0sensor\u00a0data'))

    # 4. Particle concentration at 0.3 um
    p = _safe_float(latest_rec.get('ch1_diff_m3')) if latest_rec else None
    if p is not None:
        if p > _P_HIGH:
            rows.append(('alert',
                f'\u25b2\u00a00.3\u00b5m\u00a0{p:,.0f}\u00a0/m\u00b3'
                '\u00a0\u2014\u00a0above\u00a0contamination\u00a0threshold'))
        else:
            rows.append(('ok',
                f'\u25cf\u00a00.3\u00b5m\u00a0{p:,.0f}\u00a0/m\u00b3'
                '\u00a0\u2014\u00a0within\u00a0limit'))
    else:
        rows.append(('mute', '\u25cb\u00a00.3\u00b5m:\u00a0no\u00a0data'))

    # 5. Email alerts in last 24 hr
    _labels = {
        'rh_low':          'Low\u00a0humidity',
        'rh_high':         'High\u00a0humidity',
        'temp_low':        'Low\u00a0temperature',
        'temp_high':       'High\u00a0temperature',
        'particle_high':   'High\u00a0particle\u00a0count',
        'counter_offline': 'Counter\u00a0offline',
    }
    cutoff     = datetime.now() - timedelta(hours=24)
    email_sent = False
    for ak, ats in alert_state.items():
        try:
            adt = datetime.fromisoformat(ats)
            if adt >= cutoff:
                rows.append(('email',
                    f'\u2709\u00a0Email\u00a0sent:\u00a0{_labels.get(ak, ak)}'
                    f'\u00a0at\u00a0{adt.strftime("%H:%M")}'))
                email_sent = True
        except Exception:
            pass
    if not email_sent:
        rows.append(('mute',
            '\u25cb\u00a0No\u00a0alerts\u00a0emailed\u00a0in\u00a0last\u00a024\u00a0hr'))

    # ── Determine badge state ─────────────────────────────────────────────────
    n_alert = sum(1 for s, _ in rows if s == 'alert')
    n_warn  = sum(1 for s, _ in rows if s == 'warn')

    if n_alert > 0:
        badge_color = '#f87171'
        badge_text  = f'\u26a0\u00a0{n_alert}\u00a0ALERT{"S" if n_alert > 1 else ""}'
    elif n_warn > 0:
        badge_color = '#fbbf24'
        badge_text  = f'\u26a0\u00a0{n_warn}\u00a0WARN'
    else:
        badge_color = '#4ade80'
        badge_text  = '\u25c9\u00a0OK'

    # ── Build dropdown rows HTML ──────────────────────────────────────────────
    _css_map = {
        'ok': 'ni-ok', 'warn': 'ni-warn', 'alert': 'ni-alert',
        'email': 'ni-email', 'info': 'ni-info', 'mute': 'ni-mute',
    }
    rows_html = ''.join(
        f'<div class="notif-row {_css_map.get(s, "ni-mute")}">{msg}</div>'
        for s, msg in rows
    )

    # ── CSS (single braces — normal CSS syntax) ───────────────────────────────
    css = (
        '  /* notification center badge + dropdown */\n'
        '  .notif-badge-wrap {\n'
        '    position: relative; display: inline-block;\n'
        '    align-self: flex-end; margin-bottom: 6px;\n'
        '  }\n'
        '  .notif-badge {\n'
        '    cursor: pointer; user-select: none;\n'
        '  }\n'
        '  .notif-dropdown {\n'
        '    display: none; position: absolute; right: 0; top: calc(100% + 6px);\n'
        '    z-index: 200;\n'
        '    background: #060d1a; border: 1px solid #1e3a5f;\n'
        '    border-radius: 6px; padding: 7px 14px 10px; min-width: 295px;\n'
        '    box-shadow: 0 8px 24px rgba(0,0,0,0.6);\n'
        '  }\n'
        '  .notif-dropdown.open { display: block; }\n'
        '  .notif-hdr {\n'
        '    color: #4b7ab8; font-size: 9px; text-transform: uppercase;\n'
        '    letter-spacing: 1.8px; padding-bottom: 5px;\n'
        '    border-bottom: 1px solid #1e293b; margin-bottom: 5px;\n'
        '  }\n'
        '  .notif-row {\n'
        '    font-size: 10.5px; line-height: 1.6; white-space: nowrap;\n'
        '    overflow: hidden; text-overflow: ellipsis;\n'
        '  }\n'
        '  .ni-ok    { color: #4ade80; }\n'
        '  .ni-warn  { color: #fbbf24; }\n'
        '  .ni-alert { color: #f87171; }\n'
        '  .ni-email { color: #93c5fd; }\n'
        '  .ni-info  { color: #d1d5db; }\n'
        '  .ni-mute  { color: #4b5563; }\n'
    )

    # ── Badge + dropdown HTML ─────────────────────────────────────────────────
    badge_html = (
        '<div class="notif-badge-wrap">'
        f'<div class="iso-badge notif-badge" '
        f'style="color:{badge_color};border-color:{badge_color};" '
        f'onclick="toggleNotifDropdown()" '
        f'title="System status (click to expand)">'
        f'{badge_text}</div>'
        '<div class="notif-dropdown" id="notif-dropdown">'
        '<div class="notif-hdr">\u2299\u00a0SYSTEM\u00a0STATUS</div>'
        f'{rows_html}'
        '</div>'
        '</div>'
    )

    # ── JS ────────────────────────────────────────────────────────────────────
    js = (
        'function toggleNotifDropdown() {\n'
        '  var d = document.getElementById("notif-dropdown");\n'
        '  if (d) d.classList.toggle("open");\n'
        '}\n'
        'document.addEventListener("click", function(e) {\n'
        '  var wrap = document.querySelector(".notif-badge-wrap");\n'
        '  if (wrap && !wrap.contains(e.target)) {\n'
        '    var d = document.getElementById("notif-dropdown");\n'
        '    if (d) d.classList.remove("open");\n'
        '  }\n'
        '});\n'
    )

    return {'css': css, 'badge_html': badge_html, 'js': js}
