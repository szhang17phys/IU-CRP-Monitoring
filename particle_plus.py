#!/usr/bin/env python3
"""
Particle Plus 7000 Series — noether logger + GitHub Pages dashboard
Usage:
    python3 particle_plus.py --sample     run scheduled sampling 24/7
    python3 particle_plus.py --sync       one-shot sync all records to CSV
    python3 particle_plus.py --live       stream live current data to CSV
    python3 particle_plus.py --dashboard  push CSV to GitHub and update plot
    python3 particle_plus.py --all        run everything (recommended for tmux)
"""

import argparse
import struct
import csv
import time
import os
import signal
import socket
import subprocess
from datetime import datetime, timedelta

from pymodbus.client import ModbusTcpClient

# ─── CONFIG ───────────────────────────────────────────────────────────────────

COUNTER_IP   = '10.66.66.68'
PORT         = 502

BASE_DIR     = '/home/rraut/particle_plus'   # git repo root on noether
OUTPUT_CSV   = f'{BASE_DIR}/particle_data_archive.csv'
LIVE_CSV     = f'{BASE_DIR}/particle_data_live.csv'
LOG_FILE     = f'{BASE_DIR}/sync_log.txt'
PID_FILE     = f'{BASE_DIR}/particle_plus.pid'
SESSION_FILE = f'{BASE_DIR}/session_baseline.txt'  # records pre-existing before this session

# sampling schedule
SAMPLE_TIME_S       = 60      # 1 minute sample
HOLD_TIME_S         = 1800    # 30 min between samples = twice per hour
DELAY_TIME_S        = 5       # pump stabilization
CYCLES              = 1       # 1 sample per cycle then hold

# sync/erase
ERASE_AFTER_SYNC    = False   # set True after verifying data
MIN_RECORDS_TO_SYNC = 1

# github — repo root = BASE_DIR so index.html lands at root (GitHub Pages)
GITHUB_REPO_DIR     = BASE_DIR
GITHUB_BRANCH       = 'main'
GITHUB_REMOTE       = 'origin'

# ──────────────────────────────────────────────────────────────────────────────

# ─── CONNECTION STATE ─────────────────────────────────────────────────────────
_counter_online = True
_last_seen      = None   # datetime of last successful data pull

import threading
_modbus_lock = threading.Lock()   # only one thread talks to the counter at a time
# ──────────────────────────────────────────────────────────────────────────────


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(msg, level='INFO'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


# ─── DECODERS ─────────────────────────────────────────────────────────────────
# Register map: Little-Endian across registers (word swapped)
# registers[0] = LOW word, registers[1] = HIGH word

def decode_u32(registers):
    return (registers[1] << 16) | registers[0]

def decode_i32(registers):
    raw = (registers[1] << 16) | registers[0]
    return raw - 0x100000000 if raw >= 0x80000000 else raw

def decode_float(registers):
    raw = struct.pack('>HH', registers[1], registers[0])
    return struct.unpack('>f', raw)[0]

def encode_u32(value):
    return [value & 0xFFFF, (value >> 16) & 0xFFFF]

def encode_i32(value):
    if value < 0:
        value = value & 0xFFFFFFFF
    return encode_u32(value)

def decode_string(registers):
    result = ''
    for reg in registers:
        high = (reg >> 8) & 0xFF
        low  =  reg       & 0xFF
        if high == 0:
            break
        result += chr(high)
        if low == 0:
            break
        result += chr(low)
    return result.strip()


# ─── COUNTER CONTROL ──────────────────────────────────────────────────────────

def get_state(client):
    r = client.read_holding_registers(address=5000, count=1)
    if r.isError():
        return None
    return {0:'Stopped', 1:'Delay', 2:'Counting', 3:'Hold'}.get(
        r.registers[0], f'Unknown({r.registers[0]})')

def set_params(client):
    log(f"Writing sampling params: "
        f"delay={DELAY_TIME_S}s sample={SAMPLE_TIME_S}s "
        f"hold={HOLD_TIME_S}s cycles={CYCLES}")
    client.write_registers(address=5003, values=encode_u32(DELAY_TIME_S))
    client.write_registers(address=5005, values=encode_u32(SAMPLE_TIME_S))
    client.write_registers(address=5007, values=encode_u32(HOLD_TIME_S))
    client.write_registers(address=5002, values=[CYCLES])
    time.sleep(0.5)

    # verify readback
    rd = client.read_holding_registers(address=5003, count=2)
    rs = client.read_holding_registers(address=5005, count=2)
    rh = client.read_holding_registers(address=5007, count=2)
    rc = client.read_holding_registers(address=5002, count=1)
    log(f"Verified: delay={decode_u32(rd.registers)}s "
        f"sample={decode_u32(rs.registers)}s "
        f"hold={decode_u32(rh.registers)}s "
        f"cycles={rc.registers[0]}")

def start_sampling(client):
    client.write_registers(address=5000, values=[1])
    time.sleep(1)
    state = get_state(client)
    log(f"Start command sent → state: {state}")
    return state in ('Delay', 'Counting')

def stop_sampling(client):
    client.write_registers(address=5000, values=[0])
    time.sleep(1)
    state = get_state(client)
    log(f"Stop command sent → state: {state}")
    return state == 'Stopped'

def wait_for_complete(client):
    timeout = DELAY_TIME_S + SAMPLE_TIME_S + 30
    deadline = time.time() + timeout
    log(f"Waiting for sample completion (timeout={timeout}s)...")
    while time.time() < deadline:
        state = get_state(client)
        log(f"  State: {state}")
        if state in ('Hold', 'Stopped'):
            log("Sample complete")
            return True
        if state is None:
            log("Lost connection", 'ERROR')
            return False
        time.sleep(5)
    log("Timed out waiting for sample", 'WARN')
    return False


# ─── RECORD READING ───────────────────────────────────────────────────────────

def get_record_count(client):
    r = client.read_holding_registers(address=8000, count=2)
    if r.isError():
        return 0
    return decode_u32(r.registers)

def latch_record(client, record_number):
    client.write_registers(address=8002, values=encode_i32(record_number))
    time.sleep(0.3)

def read_latched_record(client):
    data = {}

    r = client.read_holding_registers(address=9000, count=2)
    if r.isError():
        return None
    rec_num = decode_i32(r.registers)
    if rec_num == -1:
        return None
    data['record_number'] = rec_num

    r = client.read_holding_registers(address=9002, count=11)
    data['date'] = decode_string(r.registers) if not r.isError() else ''

    r = client.read_holding_registers(address=9013, count=9)
    data['time'] = decode_string(r.registers) if not r.isError() else ''

    r = client.read_holding_registers(address=9022, count=21)
    data['location'] = decode_string(r.registers) if not r.isError() else ''

    r = client.read_holding_registers(address=9074, count=2)
    data['sample_duration_s'] = (round(decode_float(r.registers), 2)
                                  if not r.isError() else None)

    r = client.read_holding_registers(address=9076, count=2)
    data['flow_CFM'] = (round(decode_float(r.registers), 4)
                        if not r.isError() else None)

    r = client.read_holding_registers(address=9078, count=1)
    if not r.isError():
        bits = r.registers[0]
        data['laser_ok'] = bool(bits & 0x0001)
        data['flow_ok']  = bool(bits & 0x0002)
        data['temp_ok']  = bool(bits & 0x0004)
        data['rh_ok']    = bool(bits & 0x0008)

    r = client.read_holding_registers(address=9079, count=1)
    if not r.isError():
        raw = r.registers[0]
        data['temp_C'] = None if raw >= 998 else round(raw * 0.1, 1)
    else:
        data['temp_C'] = None

    r = client.read_holding_registers(address=9080, count=1)
    if not r.isError():
        raw = r.registers[0]
        data['RH_pct'] = None if raw <= 1 else raw
    else:
        data['RH_pct'] = None

    # 6 channels
    for i in range(6):
        offset = i * 2
        ch     = f'ch{i+1}'

        r = client.read_holding_registers(address=10100 + offset, count=2)
        data[f'{ch}_size_um'] = (round(decode_float(r.registers), 3)
                                  if not r.isError() else None)

        r = client.read_holding_registers(address=10300 + offset, count=2)
        data[f'{ch}_diff_counts'] = (round(decode_float(r.registers), 1)
                                      if not r.isError() else None)

        r = client.read_holding_registers(address=10500 + offset, count=2)
        data[f'{ch}_diff_ft3'] = (round(decode_float(r.registers), 3)
                                   if not r.isError() else None)

        r = client.read_holding_registers(address=10700 + offset, count=2)
        data[f'{ch}_diff_m3'] = (round(decode_float(r.registers), 3)
                                  if not r.isError() else None)

        r = client.read_holding_registers(address=10900 + offset, count=2)
        data[f'{ch}_diff_mass_ugm3'] = (round(decode_float(r.registers), 6)
                                         if not r.isError() else None)

        r = client.read_holding_registers(address=11500 + offset, count=2)
        data[f'{ch}_sum_m3'] = (round(decode_float(r.registers), 3)
                                 if not r.isError() else None)

        r = client.read_holding_registers(address=11700 + offset, count=2)
        data[f'{ch}_pm_ugm3'] = (round(decode_float(r.registers), 6)
                                  if not r.isError() else None)

    return data

def read_live_snapshot(client):
    """Latch current live data (record 0) and return it"""
    latch_record(client, 0)
    data = read_latched_record(client)
    if data:
        data['snapshot_time'] = datetime.now().isoformat()
    return data


# ─── CSV ──────────────────────────────────────────────────────────────────────

def save_to_csv(records, filepath):
    if not records:
        log("No records to save")
        return False
    file_exists = os.path.exists(filepath)
    with open(filepath, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(records)
    log(f"Saved {len(records)} records → {filepath}")
    return True

def erase_counter(client):
    log("Erasing counter memory...")
    client.write_registers(address=8004, values=[0x9559])
    time.sleep(3)
    remaining = get_record_count(client)
    log(f"Records remaining: {remaining}")
    return remaining == 0


# ─── GITHUB PAGES DASHBOARD ───────────────────────────────────────────────────

def generate_dashboard_html(csv_path, output_path):
    """
    Read last 7 days of CSV data and generate a self-contained static HTML
    dashboard matching the dashboard.py visual design for GitHub Pages.
    """
    import json

    # ── read CSV ──────────────────────────────────────────────────────────────
    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    # filter last 7 days — try counter date/time, then sync_time/snapshot_time fallback
    cutoff = datetime.now() - timedelta(days=7)
    recent = []
    for row in rows:
        dt = None
        d = row.get('date', '').strip()
        t = row.get('time', '').strip()
        if d and t:
            try:
                dt = datetime.strptime(f"{d} {t}", '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        if dt is None:
            for ts_key in ('sync_time', 'snapshot_time'):
                ts_val = row.get(ts_key, '').strip()
                if ts_val:
                    try:
                        dt = datetime.fromisoformat(ts_val)
                        break
                    except Exception:
                        pass
        # include if timestamp is recent, or if no timestamp at all (unknown age)
        if dt is None or dt >= cutoff:
            recent.append(row)

    log(f"Dashboard: {len(recent)} records in last 7 days (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M:%S')})")

    # ── helpers ───────────────────────────────────────────────────────────────
    def sf(val):
        try:
            return float(val) if val not in (None, '', 'None') else None
        except Exception:
            return None

    def latest_val(key):
        for r in reversed(recent):
            v = sf(r.get(key))
            if v is not None:
                return v
        return None

    def latest_bool(key):
        for r in reversed(recent):
            v = r.get(key)
            if v not in (None, '', 'None'):
                return str(v).lower() in ('true', '1', 'yes')
        return None

    def c_to_f(c):
        return round(c * 9/5 + 32, 1) if c is not None else None

    # ── extract data ──────────────────────────────────────────────────────────
    def get_real_ts(r):
        """Return formatted timestamp string if a real one exists, else None."""
        d = r.get('date', '').strip()
        t = r.get('time', '').strip()
        if d and t:
            return f"{d} {t}"
        for key in ('sync_time', 'snapshot_time'):
            ts_val = r.get(key, '').strip()
            if ts_val:
                try:
                    return datetime.fromisoformat(ts_val).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
        return None  # no fake fallback — records without real timestamps are excluded from charts

    # session baseline: records <= baseline were on the counter before this session started;
    # their sync_time is a bulk-read timestamp, not the actual measurement time.
    # Approach: session records (> baseline) use their real sync_time; pre-session records
    # get estimated times by spacing backward at HOLD_TIME_S from the first session record.
    _session_baseline = 0
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as _sb:
                _session_baseline = int(_sb.read().strip())
        except Exception:
            pass

    # collect ALL records (ts may be None if CSV header pre-dates sync_time field)
    _all_ts = []
    for r in recent:
        ts = get_real_ts(r)  # may be None
        try:
            rec_num = int(float(r.get('record_number', 0) or 0))
        except (ValueError, TypeError):
            rec_num = 0
        _all_ts.append((rec_num, r, ts))
    _all_ts.sort(key=lambda x: x[0])

    # find anchor: first session record with a real timestamp
    _anchor_dt = None
    for _rn, _r, _ts in _all_ts:
        if _rn > _session_baseline and _ts is not None:
            try:
                _anchor_dt = datetime.strptime(_ts, '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
            break
    # fallback: last record with any real timestamp
    if _anchor_dt is None:
        for _rn, _r, _ts in reversed(_all_ts):
            if _ts is not None:
                try:
                    _anchor_dt = datetime.strptime(_ts, '%Y-%m-%d %H:%M:%S')
                    break
                except Exception:
                    pass
    # final fallback: if no real timestamps at all, anchor to now
    if _anchor_dt is None and _all_ts:
        _anchor_dt = datetime.now()

    chart_records = []
    timestamps = []
    for _rn, _r, _ts in _all_ts:
        chart_records.append(_r)
        if _rn > _session_baseline and _ts is not None:
            timestamps.append(_ts)           # accurate sync_time
        elif _anchor_dt is not None:
            # estimate: each step is HOLD_TIME_S before the anchor
            _steps = _session_baseline - _rn + 1 if _rn <= _session_baseline else 0
            _est = (_anchor_dt - timedelta(seconds=HOLD_TIME_S * _steps)).strftime('%Y-%m-%d %H:%M:%S')
            timestamps.append(_est)
        else:
            timestamps.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # ── gap-marker post-processing ───────────────────────────────────────────
    # If consecutive timestamps are > 1 hr apart, insert a None-valued sentinel
    # point one sample-period (HOLD_TIME_S) after the last real point.
    # This breaks the step chart so no stale value is held across a long gap.
    _GAP_THRESHOLD_S = 3600
    _aug_ts  = []
    _aug_rec = []
    for _gi in range(len(timestamps)):
        _aug_ts.append(timestamps[_gi])
        _aug_rec.append(chart_records[_gi])
        if _gi + 1 < len(timestamps):
            try:
                _dt_a = datetime.strptime(timestamps[_gi],     '%Y-%m-%d %H:%M:%S')
                _dt_b = datetime.strptime(timestamps[_gi + 1], '%Y-%m-%d %H:%M:%S')
                if (_dt_b - _dt_a).total_seconds() > _GAP_THRESHOLD_S:
                    _gap_ts = (_dt_a + timedelta(seconds=HOLD_TIME_S)).strftime('%Y-%m-%d %H:%M:%S')
                    _aug_ts.append(_gap_ts)
                    _aug_rec.append(None)   # sentinel — renders as blank in Plotly
            except Exception:
                pass
    _plot_timestamps = _aug_ts
    _plot_records    = _aug_rec

    ch_colors = ['#00b4d8', '#2ecc71', '#e74c3c', '#f39c12', '#9b59b6', '#1abc9c']
    pm_colors = ['#ff6b6b', '#ff9f43', '#ffd32a', '#0be881', '#67e8f9', '#c084fc']

    ref = recent[0] if recent else {}
    ch_sizes = {}
    for i in range(1, 7):
        sz = sf(ref.get(f'ch{i}_size_um'))
        ch_sizes[i] = f'{sz:.1f}' if sz is not None else str(i)

    ch_counts = {i: [sf(r.get(f'ch{i}_diff_counts')) if r is not None else None
                     for r in _plot_records] for i in range(1, 7)}
    ch_pm     = {i: [sf(r.get(f'ch{i}_pm_ugm3'))     if r is not None else None
                     for r in _plot_records] for i in range(1, 7)}
    flow_vals = [sf(r.get('flow_CFM')) if r is not None else None for r in _plot_records]

    # ── live CSV: counter only stores temp/RH in the live reading (record 0),
    #    not in historical records — read LIVE_CSV for the env chart/cards ──────
    live_cutoff = datetime.now() - timedelta(hours=24)
    live_ts      = []
    live_temp_f  = []
    live_rh_vals = []
    if os.path.exists(LIVE_CSV):
        with open(LIVE_CSV, 'r') as _lf:
            for _lr in csv.DictReader(_lf):
                _ts = _lr.get('snapshot_time', '').strip()
                if not _ts:
                    continue
                try:
                    _dt = datetime.fromisoformat(_ts)
                    if _dt >= live_cutoff:
                        live_ts.append(_dt.strftime('%Y-%m-%d %H:%M:%S'))
                        live_temp_f.append(c_to_f(sf(_lr.get('temp_C'))))
                        live_rh_vals.append(sf(_lr.get('RH_pct')))
                except Exception:
                    pass

    # ── status strip ──────────────────────────────────────────────────────────
    lv_temp_c = latest_val('temp_C')
    last_temp_f = f'{c_to_f(lv_temp_c):.1f}' if lv_temp_c is not None else '—'
    lv_rh   = latest_val('RH_pct')
    last_rh = f'{lv_rh:.1f}'  if lv_rh  is not None else '—'
    # override env cards with latest live reading if available (live has real values)
    if live_temp_f:
        _ltf = next((v for v in reversed(live_temp_f) if v is not None), None)
        if _ltf is not None:
            last_temp_f = f'{_ltf:.1f}'
    if live_rh_vals:
        _lrh = next((v for v in reversed(live_rh_vals) if v is not None), None)
        if _lrh is not None:
            last_rh = f'{_lrh:.1f}'
    lv_flow = latest_val('flow_CFM')
    last_flow = f'{lv_flow:.4f}' if lv_flow is not None else '—'
    last_ts   = timestamps[-1] if timestamps else '—'
    n_samples = len(chart_records)

    laser_ok = latest_bool('laser_ok')
    flow_ok  = latest_bool('flow_ok')
    temp_ok  = latest_bool('temp_ok')
    rh_ok    = latest_bool('rh_ok')

    def flag_span(label, ok):
        cls = '' if ok is None else ('ok' if ok else 'fail')
        txt = '—' if ok is None else ('OK' if ok else 'FAULT')
        return (f'<div class="kv"><span class="k">{label}: </span>'
                f'<span class="v {cls}">{txt}</span></div>')

    def kv_span(k, v):
        return (f'<div class="kv"><span class="k">{k}: </span>'
                f'<span class="v">{v}</span></div>')

    status_strip_html = (
        kv_span('Flow', f'{last_flow} CFM') +
        kv_span('Samples', str(n_samples)) +
        kv_span('Last sample', last_ts) +
        flag_span('Laser', laser_ok) +
        flag_span('Flow',  flow_ok)  +
        flag_span('Temp',  temp_ok)  +
        flag_span('RH',    rh_ok)
    )

    env_cards_html = ''.join(
        f'<div class="card" style="border-top:3px solid {c}">'
        f'<div class="card-label">{lab}</div>'
        f'<span class="card-val" style="color:{c}">{val}</span>'
        f'<span class="card-unit">{unit}</span></div>'
        for (lab, val, unit), c in zip(
            [('Temperature', last_temp_f, '°F'),
             ('Humidity',    last_rh,     '%'),
             ('Flow Rate',   last_flow,   'CFM')],
            ['#ff6b6b', '#4ecdc4', '#45b7d1'])
    )

    # ── pre-serialise all JS data (avoids f-string brace escaping) ────────────
    ts_js            = json.dumps(_plot_timestamps)
    counts_traces_js = json.dumps([
        {'x': _plot_timestamps, 'y': ch_counts[i],
         'name': f'\u2265{ch_sizes[i]}\u00b5m',
         'type': 'scatter', 'mode': 'lines',
         'line': {'color': ch_colors[i-1], 'width': 2, 'shape': 'hv'}}
        for i in range(1, 7)
    ])
    pm_traces_js = json.dumps([
        {'x': _plot_timestamps, 'y': ch_pm[i],
         'name': f'PM\u2265{ch_sizes[i]}\u00b5m',
         'type': 'scatter', 'mode': 'lines',
         'line': {'color': pm_colors[i-1], 'width': 2, 'shape': 'hv'}}
        for i in range(1, 7)
    ])
    raw_latest = [
        next((sf(r.get(f'ch{i}_diff_counts')) for r in reversed(recent)
              if sf(r.get(f'ch{i}_diff_counts')) is not None), 0.0) or 0.0
        for i in range(1, 7)
    ]
    dist_traces_js = json.dumps([{
        'x': [f'\u2265{ch_sizes[i]}\u00b5m' for i in range(1, 7)],
        'y': [max(v, 0.5) for v in raw_latest],
        'type': 'bar',
        'marker': {'color': ch_colors, 'line': {'color': '#334155', 'width': 1}},
        'text': [str(int(v)) if v > 0 else '0' for v in raw_latest],
        'textposition': 'outside',
        'textfont': {'color': '#9ca3af', 'size': 11},
    }])
    ch1_counts_js = json.dumps(ch_counts[1])
    ch2_pm_js     = json.dumps(ch_pm[2])
    ch1_lbl       = ch_sizes.get(1, '0.3')
    ch2_lbl       = ch_sizes.get(2, '0.5')
    live_ts_js    = json.dumps(live_ts)
    temp_f_js     = json.dumps(live_temp_f)
    rh_js         = json.dumps(live_rh_vals)

    updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── ISO 14644-1 classification ─────────────────────────────────────────────
    # Cumulative counts/m³ thresholds per ISO class for each particle size (µm)
    _ISO_TABLE = {
        0.3: [(3,102),(4,1020),(5,10200),(6,102000)],
        0.5: [(3,35),(4,352),(5,3520),(6,35200),(7,352000),(8,3520000),(9,35200000)],
        1.0: [(3,8),(4,83),(5,832),(6,8320),(7,83200),(8,832000),(9,8320000)],
        5.0: [(5,29),(6,293),(7,2930),(8,29300),(9,293000)],
    }
    _latest_rec = next((r for r in reversed(recent)), None)
    _iso_class  = None
    if _latest_rec:
        _worst = 0
        for _ci in range(1, 7):
            try:
                _sz = round(float(ch_sizes.get(_ci, '')), 1)
            except (ValueError, TypeError):
                continue
            if _sz not in _ISO_TABLE:
                continue
            _conc = sf(_latest_rec.get(f'ch{_ci}_diff_m3'))
            if _conc is None:
                continue
            _ch_cls = 10  # beyond ISO 9 until proven otherwise
            for _cls, _lim in _ISO_TABLE[_sz]:
                if _conc <= _lim:
                    _ch_cls = _cls
                    break
            if _ch_cls > _worst:
                _worst = _ch_cls
        if _worst > 0:
            _iso_class = _worst

    if _iso_class is None:
        _iso_color = '#6b7280'
        _iso_label = 'ISO —'
    elif _iso_class <= 4:
        _iso_color = '#00e676'
        _iso_label = f'ISO&nbsp;{_iso_class}'
    elif _iso_class <= 6:
        _iso_color = '#4ade80'
        _iso_label = f'ISO&nbsp;{_iso_class}'
    elif _iso_class == 7:
        _iso_color = '#facc15'
        _iso_label = f'ISO&nbsp;{_iso_class}'
    elif _iso_class == 8:
        _iso_color = '#fb923c'
        _iso_label = f'ISO&nbsp;{_iso_class}'
    else:
        _iso_color = '#f87171'
        _iso_label = f'ISO&nbsp;{_iso_class if _iso_class <= 9 else "&gt;9"}'
    iso_badge_html = (
        f'<div class="iso-badge" style="color:{_iso_color};border-color:{_iso_color};">'
        f'{_iso_label}</div>'
    )

    # ── connection banner ─────────────────────────────────────────────────────
    if _counter_online:
        conn_banner = ''
    else:
        last_seen_str = (_last_seen.strftime('%Y-%m-%d %H:%M:%S')
                         if _last_seen else 'unknown')
        conn_banner = (
            '<div style="background:#7f1d1d;border:1px solid #991b1b;'
            'color:#fca5a5;border-radius:6px;padding:10px 16px;'
            'margin-bottom:16px;font-size:13px;">'
            f'&#9888; Particle counter OFFLINE &mdash; '
            f'last connected: {last_seen_str} &mdash; '
            'retrying every 30 min. Showing last recorded data.</div>'
        )

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>Wright Lab &#8212; DUNE Clean Room Monitor</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #030712;
    color: #d1d5db;
    font-family: 'Courier New', Courier, monospace;
    padding: 20px 28px 40px;
    min-height: 100vh;
  }}
  .header {{ margin-bottom: 20px; border-bottom: 1px solid #1f2937; padding-bottom: 14px; }}
  .header h1 {{
    color: #38bdf8;
    font-size: 18px;
    letter-spacing: 3px;
    font-weight: bold;
    margin-bottom: 3px;
  }}
  .header .sub {{ color: #6b7280; font-size: 11px; letter-spacing: 1px; }}
  .controls {{
    display: flex; gap: 16px; align-items: flex-end;
    flex-wrap: wrap; margin-bottom: 14px;
  }}
  .ctrl-group label {{
    display: block; font-size: 10px; color: #6b7280;
    letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px;
  }}
  select {{
    background: #111827; color: #d1d5db; border: 1px solid #374151;
    border-radius: 5px; padding: 6px 10px; font-size: 13px;
    font-family: inherit; cursor: pointer; min-width: 180px;
  }}
  select:focus {{ outline: none; border-color: #38bdf8; }}
  .updated {{ font-size: 11px; color: #4b5563; align-self: flex-end; padding-bottom: 6px; }}
  .iso-badge {{
    display: inline-block; align-self: flex-end; margin-bottom: 6px;
    font-size: 14px; font-weight: bold; letter-spacing: 3px;
    border: 1.5px solid; border-radius: 6px;
    padding: 4px 16px; font-family: inherit;
  }}
  .status-strip {{
    display: flex; gap: 20px; flex-wrap: wrap;
    background: #0f172a; border: 1px solid #1f2937;
    border-radius: 7px; padding: 10px 18px;
    margin-bottom: 14px; font-size: 12px;
  }}
  .kv .k {{ color: #6b7280; }}
  .kv .v {{ font-weight: bold; color: #d1d5db; }}
  .kv .ok   {{ color: #4ade80; }}
  .kv .fail {{ color: #f87171; }}
  .cards {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }}
  .card {{
    flex: 1; min-width: 120px; background: #0f172a;
    border: 1px solid #1f2937; border-radius: 7px; padding: 12px 16px;
  }}
  .card .card-label {{
    font-size: 10px; color: #6b7280; text-transform: uppercase;
    letter-spacing: 1.2px; margin-bottom: 6px;
  }}
  .card .card-val {{ font-size: 26px; font-weight: bold; line-height: 1; }}
  .card .card-unit {{ font-size: 12px; color: #6b7280; margin-left: 3px; }}
  .chart-panel {{
    background: #0f172a; border: 1px solid #1f2937;
    border-radius: 8px; padding: 14px 14px 6px; margin-bottom: 12px;
  }}
  .chart-title {{
    font-size: 10px; color: #6b7280; text-transform: uppercase;
    letter-spacing: 1.5px; margin-bottom: 6px;
  }}
  .row2 {{ display: flex; gap: 12px; margin-bottom: 12px; }}
  .row2 .chart-panel {{ flex: 1; margin-bottom: 0; }}
</style>
</head>
<body>

<div class="header">
  <h1>WRIGHT LAB &#8212; HIGH BAY DUNE CLEAN ROOM MONITORING</h1>
  <div class="sub">Particle Plus Model 7301 &nbsp;&middot;&nbsp; Real-time Particulate &amp; Environmental Dashboard</div>
</div>

{conn_banner}

<div class="controls">
  <div class="ctrl-group">
    <label>Time Range</label>
    <select id="sel-range" onchange="filterAndRender()">
      <option value="0" selected>All data (7 days)</option>
      <option value="30">Last 30 min</option>
      <option value="60">Last 1 hr</option>
      <option value="120">Last 2 hr</option>
      <option value="180">Last 3 hr</option>
      <option value="360">Last 6 hr</option>
      <option value="720">Last 12 hr</option>
      <option value="1440">Last 24 hr</option>
      <option value="2880">Last 2 days</option>
    </select>
  </div>
  {iso_badge_html}
  <div class="updated">Last pushed: {updated}</div>
</div>

<div class="status-strip">{status_strip_html}</div>
<div class="cards">{env_cards_html}</div>

<div class="chart-panel">
  <div class="chart-title">Particle Counts Over Time &nbsp;&#8212; all 6 size channels (log scale, counts / sample)</div>
  <div id="chart-counts" style="height:360px"></div>
</div>

<div class="chart-panel">
  <div class="chart-title">PM Mass Concentration Over Time &nbsp;(&#956;g / m&#179;)</div>
  <div id="chart-pm" style="height:300px"></div>
</div>

<div class="row2">
  <div class="chart-panel">
    <div class="chart-title">Latest Particle Size Distribution &nbsp;(most recent sample)</div>
    <div id="chart-dist" style="height:280px"></div>
  </div>
  <div class="chart-panel">
    <div class="chart-title">Temperature &amp; Humidity Over Time</div>
    <div id="chart-env" style="height:280px"></div>
  </div>
</div>

<script>
const TS     = {ts_js};
const COUNTS = {counts_traces_js};
const PM     = {pm_traces_js};
const DIST   = {dist_traces_js};
const CH1_C  = {ch1_counts_js};
const CH2_PM = {ch2_pm_js};
const LIVE_TS = {live_ts_js};
const TEMP_F  = {temp_f_js};
const RH_VALS = {rh_js};

const DARK = {{
  paper_bgcolor: '#0f172a',
  plot_bgcolor:  '#0f172a',
  font:      {{ color: '#9ca3af', family: 'Courier New, monospace', size: 11 }},
  margin:    {{ l: 60, r: 20, t: 30, b: 50 }},
  hovermode: 'x unified',
  hoverlabel: {{ bgcolor: '#1e293b', bordercolor: '#334155', font: {{ size: 11 }} }},
  legend: {{ bgcolor: 'rgba(0,0,0,0)', bordercolor: '#334155', borderwidth: 1,
             font: {{ size: 11 }}, orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0 }},
  xaxis: {{ gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: {{ color: '#6b7280', size: 10 }},
           title_font: {{ color: '#6b7280', size: 11 }} }},
  yaxis: {{ gridcolor: '#1e293b', linecolor: '#334155', zerolinecolor: '#1e293b',
           tickfont: {{ color: '#6b7280', size: 10 }},
           title_font: {{ color: '#6b7280', size: 11 }} }},
}};

function sliceIdx(mins) {{
  if (!mins || TS.length === 0) return 0;
  const cut = new Date(new Date(TS[TS.length - 1]) - mins * 60000);
  const i = TS.findIndex(t => new Date(t) >= cut);
  return i < 0 ? TS.length - 1 : i;
}}

function sliceTraces(traces, i) {{
  return traces.map(tr => Object.assign({{}}, tr, {{
    x: tr.x.slice(i), y: tr.y.slice(i)
  }}));
}}

function filterAndRender() {{
  const mins = parseInt(document.getElementById('sel-range').value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);

  Plotly.react('chart-counts', sliceTraces(COUNTS, i),
    Object.assign({{}}, DARK, {{
      yaxis: Object.assign({{}}, DARK.yaxis, {{ title: 'Counts / sample', type: 'log' }}),
      xaxis: Object.assign({{}}, DARK.xaxis, {{ title: '' }}),
    }}), {{responsive: true, displaylogo: false}});

  Plotly.react('chart-pm', sliceTraces(PM, i),
    Object.assign({{}}, DARK, {{
      yaxis: Object.assign({{}}, DARK.yaxis, {{ title: '\u03bcg / m\u00b3' }}),
      xaxis: Object.assign({{}}, DARK.xaxis, {{ title: '' }}),
    }}), {{responsive: true, displaylogo: false}});

  Plotly.react('chart-dist', DIST,
    Object.assign({{}}, DARK, {{
      showlegend: false, bargap: 0.3,
      yaxis: Object.assign({{}}, DARK.yaxis, {{ title: 'Counts', type: 'log', range: [-0.5, null] }}),
      xaxis: Object.assign({{}}, DARK.xaxis, {{ title: 'Particle Size' }}),
    }}), {{responsive: true, displaylogo: false}});

  const livei = (LIVE_TS.length === 0 || !mins) ? 0 : (() => {{
    const cut = new Date(new Date(LIVE_TS[LIVE_TS.length - 1]) - mins * 60000);
    const j = LIVE_TS.findIndex(t => new Date(t) >= cut);
    return j < 0 ? LIVE_TS.length - 1 : j;
  }})();
  Plotly.react('chart-env', [
    {{ x: LIVE_TS.slice(livei), y: TEMP_F.slice(livei),  name: 'Temperature (\u00b0F)',
       type: 'scatter', mode: 'lines',
       line: {{ color: '#ff6b6b', width: 2 }}, yaxis: 'y' }},
    {{ x: LIVE_TS.slice(livei), y: RH_VALS.slice(livei), name: 'Humidity (%)',
       type: 'scatter', mode: 'lines',
       line: {{ color: '#4ecdc4', width: 2 }}, yaxis: 'y2' }},
  ], Object.assign({{}}, DARK, {{
    xaxis:  Object.assign({{}}, DARK.xaxis,  {{ title: '' }}),
    yaxis:  Object.assign({{}}, DARK.yaxis,  {{ title: 'Temperature (\u00b0F)' }}),
    yaxis2: {{ title: 'Humidity (%)',
               overlaying: 'y', side: 'right',
               gridcolor: '#1e293b', linecolor: '#334155',
               tickfont: {{ color: '#6b7280', size: 10 }},
               title_font: {{ color: '#6b7280', size: 11 }} }},
  }}), {{responsive: true, displaylogo: false}});
}}

filterAndRender();
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(html)
    log(f"Dashboard HTML written → {output_path}")
    return True


def push_to_github(repo_dir, csv_path):
    """
    Copy CSV + generated HTML into the repo, commit, and push.
    Requires the repo to already be cloned and have push access
    via SSH key or token.
    """
    import shutil

    html_path = os.path.join(repo_dir, 'index.html')
    csv_dest  = os.path.join(repo_dir, 'data', 'particle_data.csv')

    os.makedirs(os.path.join(repo_dir, 'data'), exist_ok=True)

    # generate fresh dashboard
    generate_dashboard_html(csv_path, html_path)

    # copy latest CSV into repo (skip if not yet created)
    if os.path.exists(csv_path):
        shutil.copy2(csv_path, csv_dest)
        log(f"Copied CSV → {csv_dest}")
    else:
        log(f"CSV not yet created, skipping copy")

    # git add + commit + push
    cmds = [
        ['git', '-C', repo_dir, 'add', '-A'],
        ['git', '-C', repo_dir, 'commit', '-m',
         f'Auto-update {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'],
        ['git', '-C', repo_dir, 'push', GITHUB_REMOTE, GITHUB_BRANCH],
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # commit returns 1 if nothing to commit — that's ok
            if 'nothing to commit' in result.stdout:
                log("Nothing new to commit to GitHub")
                return True
            log(f"Git error: {result.stderr}", 'ERROR')
            return False
        log(f"Git: {' '.join(cmd[2:])} → OK")

    log("Dashboard pushed to GitHub Pages")
    return True


# ─── MODE FUNCTIONS ───────────────────────────────────────────────────────────

def connect():
    client = ModbusTcpClient(COUNTER_IP, port=PORT, timeout=5)
    if not client.connect():
        log("Connection failed", 'ERROR')
        return None
    log(f"Connected to {COUNTER_IP}:{PORT}")
    return client


def mode_sample():
    """
    Main 24/7 sampling loop.
    - Sets sampling params on counter
    - Starts sampling
    - Waits for completion
    - Syncs records to CSV
    - Pushes dashboard to GitHub
    - Sleeps until next cycle
    """
    log("="*55)
    log("MODE: --sample  (24/7 scheduler)")
    log(f"  Sampling every {HOLD_TIME_S}s ({HOLD_TIME_S//60} min)")
    log("="*55)

    global _counter_online, _last_seen
    params_written = False

    while True:
        with _modbus_lock:
            client = connect()
            if client is None:
                _counter_online = False
                log(f"Connection failed — pushing last-known dashboard, retrying in {HOLD_TIME_S}s...")
                mode_dashboard()
            else:
                try:
                    _counter_online = True

                    if not params_written:
                        set_params(client)
                        params_written = True

                    state = get_state(client)
                    log(f"State: {state}")

                    if state == 'Stopped':
                        start_sampling(client)

                    completed = wait_for_complete(client)

                    if completed:
                        mode_sync(client=client)
                        _last_seen = datetime.now()
                        mode_dashboard()

                except Exception as e:
                    log(f"Error in sample loop: {e}", 'ERROR')
                finally:
                    client.close()

        log(f"Sleeping {HOLD_TIME_S}s until next sample...")
        time.sleep(HOLD_TIME_S)


def mode_sync(client=None):
    """Pull all records from counter → CSV, optionally erase"""
    log("MODE: --sync")
    own_client = client is None
    if own_client:
        client = connect()
        if client is None:
            return False

    try:
        total = get_record_count(client)
        log(f"Records on counter: {total}")

        # find the highest record_number already saved so we only pull NEW records
        last_saved = 0
        if os.path.exists(OUTPUT_CSV):
            with open(OUTPUT_CSV, 'r') as f:
                for row in csv.DictReader(f):
                    try:
                        n = int(float(row.get('record_number', 0) or 0))
                        if n > last_saved:
                            last_saved = n
                    except (ValueError, TypeError):
                        pass

        if last_saved >= total:
            log(f"Already up to date (saved up to record {last_saved}, counter has {total})")
            return True

        start    = last_saved + 1
        n_new    = total - last_saved
        log(f"New records to sync: {n_new}  (records {start}–{total})")

        if n_new < MIN_RECORDS_TO_SYNC:
            log("Below sync threshold, skipping")
            return True

        records = []
        failed  = []

        for i in range(start, total + 1):
            try:
                latch_record(client, i)
                data = read_latched_record(client)
                if data:
                    data['sync_time'] = datetime.now().isoformat()
                    records.append(data)
                    log(f"  [{i:4d}/{total}] (new {i-last_saved}/{n_new}) "
                        f"temp={data.get('temp_C','?')}C "
                        f"ch1={data.get('ch1_diff_m3','?')}/m³")
                else:
                    failed.append(i)
            except Exception as e:
                log(f"  [{i:4d}/{total}] Error: {e}", 'ERROR')
                failed.append(i)

        saved = save_to_csv(records, OUTPUT_CSV)

        if failed:
            log(f"WARNING: {len(failed)} failed — NOT erasing", 'WARN')
            return False

        if ERASE_AFTER_SYNC and saved:
            erase_counter(client)

        return True

    finally:
        if own_client:
            client.close()


def mode_live():
    """
    Continuously snapshot live (in-progress) data to LIVE_CSV.
    Useful for watching what the counter is currently seeing.
    """
    log("MODE: --live  (streaming live snapshots every 10s)")
    log(f"  Output: {LIVE_CSV}")
    log("  Ctrl+C to stop")

    while True:
        # skip this cycle if the sample thread is using the counter
        if not _modbus_lock.acquire(blocking=False):
            time.sleep(10)
            continue
        try:
            client = connect()
            if client is None:
                time.sleep(30)
                continue
            try:
                data = read_live_snapshot(client)
                if data:
                    save_to_csv([data], LIVE_CSV)
                    log(f"Live: temp={data.get('temp_C')}C "
                        f"RH={data.get('RH_pct')}% "
                        f"ch1_diff_m3={data.get('ch1_diff_m3')}")
            except Exception as e:
                log(f"Live error: {e}", 'ERROR')
            finally:
                client.close()
        finally:
            _modbus_lock.release()
        time.sleep(10)


def mode_dashboard():
    """Generate HTML and push to GitHub Pages"""
    log("MODE: --dashboard")
    push_to_github(GITHUB_REPO_DIR, OUTPUT_CSV)


def mode_all():
    """
    Run sampling + live streaming + dashboard updates together.
    Recommended for the tmux session on noether.
    """
    import threading

    log("MODE: --all  (sample + live + dashboard)")

    # write PID so --stop can find and signal this process
    with open(PID_FILE, 'w') as _pf:
        _pf.write(str(os.getpid()))

    # record how many records the counter already had before this session —
    # those are pre-existing data of unknown age and will be excluded from charts
    try:
        _cl = connect()
        if _cl:
            _baseline = get_record_count(_cl)
            _cl.close()
            with open(SESSION_FILE, 'w') as _sf:
                _sf.write(str(_baseline))
            log(f"Session baseline: {_baseline} pre-existing counter records (excluded from charts)")
    except Exception as _e:
        log(f"Could not read session baseline: {_e}", 'WARN')

    try:
        t_live = threading.Thread(target=mode_live, daemon=True)
        t_live.start()

        # main thread runs the scheduler (includes dashboard push after each sync)
        mode_sample()
    finally:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Particle Plus 7000 Series logger for noether cluster')
    parser.add_argument('--sample',    action='store_true',
                        help='Run 24/7 sampling scheduler')
    parser.add_argument('--sync',      action='store_true',
                        help='One-shot: pull all records to CSV')
    parser.add_argument('--live',      action='store_true',
                        help='Stream live current data to CSV')
    parser.add_argument('--dashboard', action='store_true',
                        help='Generate HTML and push to GitHub Pages')
    parser.add_argument('--all',       action='store_true',
                        help='Run everything (recommended for tmux)')
    parser.add_argument('--stop',      action='store_true',
                        help='Gracefully stop a running --all instance')
    args = parser.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)

    if args.stop:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as _pf:
                _pid = int(_pf.read().strip())
            os.kill(_pid, signal.SIGTERM)
            print(f"Stop signal sent to particle monitor (PID {_pid}).")
            print("The current sample cycle will finish, then the process will exit.")
        else:
            print("No running particle monitor found (no PID file).")
        return
    elif args.sample:
        mode_sample()
    elif args.sync:
        mode_sync()
    elif args.live:
        mode_live()
    elif args.dashboard:
        mode_dashboard()
    elif args.all:
        mode_all()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
