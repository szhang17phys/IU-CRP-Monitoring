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

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
DATA_DIR         = f'{BASE_DIR}/data'
ARCHIVE_CSV      = f'{DATA_DIR}/measurement_archive.csv'  # all data, local only
LIVE_CSV         = f'{DATA_DIR}/live.csv'                 # 30-day particle window, GitHub
ENV_SNAPSHOT_CSV = f'{DATA_DIR}/env_live.csv'             # 10s env snapshots, GitHub
COUNTER_STATE    = f'{DATA_DIR}/counter_state.json'       # tracks last synced record
SESSION_FILE     = f'{DATA_DIR}/session_baseline.txt'
LOG_FILE         = f'{BASE_DIR}/sync_log.txt'
PID_FILE         = f'{BASE_DIR}/particle_plus.pid'

# sampling schedule
SAMPLE_TIME_S       = 60      # 1 minute sample
HOLD_TIME_S         = 240     # 4 min between samples = ~15 per hour
DELAY_TIME_S        = 5       # pump stabilization
CYCLES              = 1       # 1 sample per cycle then hold

# sync/erase
ERASE_AFTER_SYNC    = False   # set True after verifying data
MIN_RECORDS_TO_SYNC = 1
TRIM_CAP            = 10_000  # auto-erase when counter exceeds this many records

# github — repo root = BASE_DIR so index.html lands at root (GitHub Pages)
GITHUB_REPO_DIR     = BASE_DIR
GITHUB_BRANCH       = 'main'
GITHUB_REMOTE       = 'origin'

# Counter admin password — must be written to register 1000 BEFORE any Protected
# Write (PW) register can be written.  Registers 1016 (Date) and 1027 (Time) are
# R+PW, so without this the clock writes silently fail and date/time stay empty.
# Default is empty string (no password set).  Change if a password was configured.
COUNTER_PASSWORD    = ''

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
    # Device stores one ASCII character per register in the LOW byte (high byte is always 0x00)
    result = ''
    for reg in registers:
        low = reg & 0xFF
        if low == 0:
            break
        result += chr(low)
    return result.strip()

def encode_string(text, num_regs):
    """Encode a string into Modbus registers (one char per register in the low byte, zero-terminated)."""
    regs = []
    for i in range(num_regs):
        low = ord(text[i]) if i < len(text) else 0
        regs.append(low)
    return regs


# ─── COUNTER CONTROL ──────────────────────────────────────────────────────────

def get_state(client):
    r = client.read_holding_registers(address=5000, count=1)
    if r.isError():
        return None
    return {0:'Stopped', 1:'Delay', 2:'Counting', 3:'Hold'}.get(
        r.registers[0], f'Unknown({r.registers[0]})')

def set_params(client):
    sync_counter_clock(client)
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

def sync_counter_clock(client):
    """Sync the counter's RTC to Pi system time.

    Registers 1016 (Date) and 1027 (Time) are Protected Write (R+PW):
    admin password must be written to reg 1000 first.
    Device Status reg 5001 bit 0x0004 = 'Time of day clock not running'.
    """
    # ── 1. Read Device Status 5001 (log full value for diagnostics) ────────────
    rs = client.read_holding_registers(address=5001, count=1)
    if rs.isError():
        log("sync_counter_clock: cannot read Device Status (reg 5001)", 'WARN')
    else:
        dev_status = rs.registers[0]
        log(f"sync_counter_clock: Device Status 5001 = 0x{dev_status:04X}  "
            f"(clock_not_running={bool(dev_status & 0x0004)}, "
            f"flow_err={bool(dev_status & 0x0001)}, "
            f"laser_err={bool(dev_status & 0x0002)})")
        if dev_status & 0x0004:
            log("Counter RTC hardware not running — cannot set clock via Modbus", 'WARN')
            return

    # ── 2. Read current state of 1016/1027 BEFORE write ───────────────────────
    pre_d = client.read_holding_registers(address=1016, count=11)
    pre_t = client.read_holding_registers(address=1027, count=9)
    pre_date = decode_string(pre_d.registers) if not pre_d.isError() else '?'
    pre_time = decode_string(pre_t.registers) if not pre_t.isError() else '?'
    log(f"sync_counter_clock: clock BEFORE write = '{pre_date}' '{pre_time}'  "
        f"raw_regs_1016={pre_d.registers[:3] if not pre_d.isError() else '?'}")

    now      = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    time_str = now.strftime('%H:%M:%S')

    try:
        # ── 3. Authenticate: write admin password to register 1000 ─────────────
        pw_regs = encode_string(COUNTER_PASSWORD, 16)
        log(f"sync_counter_clock: writing password to reg 1000  "
            f"(COUNTER_PASSWORD={repr(COUNTER_PASSWORD)}, "
            f"first_reg=0x{pw_regs[0]:04X})")
        client.write_registers(address=1000, values=pw_regs)
        time.sleep(0.2)

        # ── 4. Write date (reg 1016) and time (reg 1027) ───────────────────────
        log(f"sync_counter_clock: writing date '{date_str}' → reg 1016, "
            f"time '{time_str}' → reg 1027")
        client.write_registers(address=1016, values=encode_string(date_str, 11))
        time.sleep(0.2)
        client.write_registers(address=1027, values=encode_string(time_str, 9))
        time.sleep(0.3)

        # ── 5. Read back and compare ───────────────────────────────────────────
        rd = client.read_holding_registers(address=1016, count=11)
        rt = client.read_holding_registers(address=1027, count=9)
        rb_date = decode_string(rd.registers) if not rd.isError() else '?'
        rb_time = decode_string(rt.registers) if not rt.isError() else '?'
        log(f"sync_counter_clock: clock AFTER write = '{rb_date}' '{rb_time}'")

        if rb_date == date_str and rb_time == time_str:
            log(f"Counter clock synced OK: {date_str} {time_str}")
        else:
            log(f"Counter clock sync FAILED — "
                f"sent '{date_str}' '{time_str}' | "
                f"readback '{rb_date}' '{rb_time}' | "
                f"pre-write was '{pre_date}' '{pre_time}'. "
                f"If readback == pre-write the write was rejected — "
                f"check COUNTER_PASSWORD (currently {repr(COUNTER_PASSWORD)}). "
                f"Find the admin password in the device's Settings menu "
                f"(Communications → Admin Password) and set COUNTER_PASSWORD.", 'WARN')
    except Exception as e:
        log(f"sync_counter_clock: exception: {e}", 'WARN')

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
        data['laser_ok']        = bool(bits & 0x0001)
        data['flow_ok']         = bool(bits & 0x0002)
        data['temp_ok']         = bool(bits & 0x0004)
        data['rh_ok']           = bool(bits & 0x0008)
        data['timestamp_valid'] = not bool(bits & 0x0080)  # 0x0080 = "Timestamp is invalid"

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
    if remaining == 0:
        from features.data_manager import reset_sync_state
        reset_sync_state(COUNTER_STATE)
        log("Sync state reset to 0 after erase")
    return remaining == 0


# ─── GITHUB PAGES DASHBOARD ───────────────────────────────────────────────────

def generate_dashboard_html(csv_path, output_path):
    """
    Read last 30 days of CSV data and generate a self-contained static HTML
    dashboard matching the dashboard.py visual design for GitHub Pages.
    """
    import json

    # ── read CSV ──────────────────────────────────────────────────────────────
    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    # filter last 30 days — live.csv is already trimmed; this catches edge cases
    cutoff = datetime.now() - timedelta(days=30)
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

    log(f"Dashboard: {len(recent)} records in last 30 days (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M:%S')})")

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
        # Only trust date/time from counter if timestamp_valid is True (or absent for old records)
        ts_valid = r.get('timestamp_valid', None)
        if d and t and ts_valid is not False:
            return f"{d} {t}"
        for key in ('sync_time', 'snapshot_time'):
            ts_val = r.get(key, '').strip()
            if ts_val:
                try:
                    return datetime.fromisoformat(ts_val).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
        return None  # no fake fallback — records without real timestamps are excluded from charts

    # ── batch-aware timestamp assignment ─────────────────────────────────────
    # The counter never populates its internal date/time fields; the only
    # timestamp available is sync_time (when the Pi read the record via Modbus).
    #
    # Two modes of syncing:
    #   • Bulk sync  — many records read back-to-back; consecutive sync_times
    #                  are only ~0.35 s apart.  sync_time is meaningless as a
    #                  measurement timestamp; we estimate backward instead.
    #   • Individual — one new record synced right after a sample completes;
    #                  sync_time ≈ actual measurement time (accurate).
    #
    # Detection: if two consecutive records have sync_times within
    # _BULK_THRESHOLD_S of each other, they belong to the same bulk batch.
    # Otherwise each record is an individual sync.
    #
    # For bulk batches: anchor = last record's sync_time, then space all
    # records backward at HOLD_TIME_S intervals (one step per record).
    # For individual records: use sync_time directly.

    _BULK_THRESHOLD_S = 60   # <60 s gap between consecutive sync_times → bulk

    # collect records sorted by record_number
    _all_ts = []
    for r in recent:
        ts = get_real_ts(r)
        try:
            rec_num = int(float(r.get('record_number', 0) or 0))
        except (ValueError, TypeError):
            rec_num = 0
        # parse datetime for gap detection
        _dt = None
        if ts:
            try:
                _dt = datetime.fromisoformat(ts.replace(' ', 'T').split('+')[0])
            except Exception:
                pass
        _all_ts.append((rec_num, r, ts, _dt))
    _all_ts.sort(key=lambda x: x[0])

    # split into batches
    _batches = []
    _cur = []
    for item in _all_ts:
        if not _cur:
            _cur.append(item)
        else:
            _prev_dt = next((x[3] for x in reversed(_cur) if x[3] is not None), None)
            _this_dt = item[3]
            if (_prev_dt is not None and _this_dt is not None and
                    (_this_dt - _prev_dt).total_seconds() <= _BULK_THRESHOLD_S):
                _cur.append(item)          # same bulk batch
            else:
                _batches.append(_cur)
                _cur = [item]              # start new batch
    if _cur:
        _batches.append(_cur)

    # assign timestamps batch by batch
    chart_records = []
    timestamps    = []
    for _batch in _batches:
        _n = len(_batch)
        _anchor = next((x[3] for x in reversed(_batch) if x[3] is not None), None) \
                  or datetime.now()
        if _n <= 2:
            # individual sync(s) — sync_time ≈ measurement time
            for _rn, _r, _ts, _dt in _batch:
                timestamps.append(_anchor.strftime('%Y-%m-%d %H:%M:%S')
                                   if _dt is None else _dt.strftime('%Y-%m-%d %H:%M:%S'))
                chart_records.append(_r)
        else:
            # bulk batch — estimate backward from last record's sync_time
            _est_ts = [
                (_anchor - timedelta(seconds=HOLD_TIME_S * (_n - 1 - _i)))
                .strftime('%Y-%m-%d %H:%M:%S')
                for _i in range(_n)
            ]
            timestamps.extend(_est_ts)
            chart_records.extend([x[1] for x in _batch])

    # Step-hold: use real timestamps directly — with line.shape='hv' in Plotly,
    # each measured value is held horizontally until the next measurement arrives.
    # No gap sentinels: a long offline period shows as a flat held line, which
    # correctly represents "last known value" rather than a misleading blank.
    # Sort chronologically so the JS time-range filter (sliceIdx) works correctly.
    if timestamps:
        _paired = sorted(zip(timestamps, chart_records), key=lambda x: x[0])
        _plot_timestamps = [x[0] for x in _paired]
        _plot_records    = [x[1] for x in _paired]
    else:
        _plot_timestamps = timestamps
        _plot_records    = chart_records

    ch_colors = ['#00b4d8', '#2ecc71', '#e74c3c', '#f39c12', '#9b59b6', '#1abc9c']
    pm_colors = ['#ff6b6b', '#ff9f43', '#ffd32a', '#0be881', '#67e8f9', '#c084fc']

    ref = recent[0] if recent else {}
    ch_sizes = {}
    for i in range(1, 7):
        sz = sf(ref.get(f'ch{i}_size_um'))
        ch_sizes[i] = f'{sz:.1f}' if sz is not None else str(i)

    ch_counts = {i: [sf(r.get(f'ch{i}_diff_m3')) if r is not None else None
                     for r in _plot_records] for i in range(1, 7)}
    ch_pm     = {i: [sf(r.get(f'ch{i}_pm_ugm3'))     if r is not None else None
                     for r in _plot_records] for i in range(1, 7)}
    flow_vals = [sf(r.get('flow_CFM')) if r is not None else None for r in _plot_records]

    # ── env snapshot CSV: counter only stores temp/RH in the live reading (record 0),
    #    not in historical records — read ENV_SNAPSHOT_CSV for the env chart/cards ──
    live_cutoff = datetime.now() - timedelta(hours=24)
    live_ts      = []
    live_temp_f  = []
    live_rh_vals = []
    if os.path.exists(ENV_SNAPSHOT_CSV):
        with open(ENV_SNAPSHOT_CSV, 'r') as _lf:
            _raw = csv.reader(_lf)
            _hdr = next(_raw, [])
            # snapshot_time is always the last column regardless of row width.
            # temp_C / RH_pct are at fixed header positions, but an extra
            # timestamp_valid column was added mid-stream to newer rows,
            # shifting every field after rh_ok by 1.  Detect and correct.
            _tc_col = _hdr.index('temp_C') if 'temp_C' in _hdr else None
            _rh_col = _hdr.index('RH_pct') if 'RH_pct' in _hdr else None
            for _row in _raw:
                if not _row:
                    continue
                _ts = _row[-1].strip()   # snapshot_time is always last
                if not _ts:
                    continue
                try:
                    _dt = datetime.fromisoformat(_ts)
                    if _dt >= live_cutoff:
                        _shift = len(_row) - len(_hdr)   # 0 old rows, 1 new rows
                        _tc_raw = _row[_tc_col + _shift] if _tc_col is not None else None
                        _rh_raw = _row[_rh_col + _shift] if _rh_col is not None else None
                        live_ts.append(_dt.strftime('%Y-%m-%d %H:%M:%S'))
                        live_temp_f.append(c_to_f(sf(_tc_raw)))
                        live_rh_vals.append(sf(_rh_raw))
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

    # ISO 14644-1:2015 concentration limits (counts/m³) for the 0.5 µm channel.
    # These are added as reference lines on the particle count chart so the
    # measured concentrations can be compared directly to the standard.
    # Colors match the \u22650.5 \u00b5m channel trace (#2ecc71 = ch2) since all limits
    # are defined at that size. ISO 6 is the exact channel color and boldest.
    _iso_ref_lines = [
        {'y': 3520,     'label': 'ISO\u00a05',  'color': '#a7f3d0', 'width': 1.5, 'dash': 'dash', 'bold': False},
        {'y': 35200,    'label': 'ISO\u00a06',  'color': '#2ecc71', 'width': 2.5, 'dash': 'dash', 'bold': True},
        {'y': 352000,   'label': 'ISO\u00a07',  'color': '#34d399', 'width': 1.5, 'dash': 'dash', 'bold': False},
        {'y': 3520000,  'label': 'ISO\u00a08',  'color': '#10b981', 'width': 1.5, 'dash': 'dash', 'bold': False},
        {'y': 35200000, 'label': 'ISO\u00a09',  'color': '#059669', 'width': 1.5, 'dash': 'dash', 'bold': False},
    ]
    iso_lines_js = json.dumps(_iso_ref_lines)

    updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── ISO 14644-1:2015 classification ───────────────────────────────────────
    # Keyed by (class, size_um) → max cumulative particles/m³.
    # Only sizes with defined limits at each class are included.
    # ISO 7-9 have no ≥0.3 µm limit, so exceeding ISO 6 at 0.3 µm does NOT
    # make the room "ISO 9" — it only disqualifies ISO 1-6 at that size.
    _ISO_FULL = {
        (3, 0.3): 102,       (3, 0.5): 35,        (3, 1.0): 8,
        (4, 0.3): 1020,      (4, 0.5): 352,        (4, 1.0): 83,
        (5, 0.3): 10200,     (5, 0.5): 3520,       (5, 1.0): 832,     (5, 5.0): 29,
        (6, 0.3): 102000,    (6, 0.5): 35200,      (6, 1.0): 8320,    (6, 5.0): 293,
        (7,       0.5): 352000,    (7, 1.0): 83200,    (7, 5.0): 2930,
        (8,       0.5): 3520000,   (8, 1.0): 832000,   (8, 5.0): 29300,
        (9,       0.5): 35200000,  (9, 1.0): 8320000,  (9, 5.0): 293000,
    }
    _latest_rec = next((r for r in reversed(recent)), None)
    _iso_class  = None
    if _latest_rec:
        # Use cumulative (sum) counts — the standard specifies ≥ particle size
        _measured = {}
        for _ci in range(1, 7):
            try:
                _sz = round(float(ch_sizes.get(_ci, '')), 1)
            except (ValueError, TypeError):
                continue
            _conc = sf(_latest_rec.get(f'ch{_ci}_sum_m3'))
            if _conc is not None:
                _measured[_sz] = _conc

        # Find the most stringent (lowest-numbered) class where every channel
        # with a defined limit at that class meets its limit.
        for _cls in range(1, 10):
            _applicable = [(sz, lim) for (c, sz), lim in _ISO_FULL.items()
                           if c == _cls and sz in _measured]
            if not _applicable:
                continue
            if all(_measured[sz] <= lim for sz, lim in _applicable):
                _iso_class = _cls
                break

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
        _iso_label = 'ISO&nbsp;9'
    iso_badge_html = (
        f'<div class="iso-badge" style="color:{_iso_color};border-color:{_iso_color};margin-left:14px;">'
        f'{_iso_label}</div>'
    )

    # ── notification center ────────────────────────────────────────────────────
    # Thresholds mirror features/alerts/alerts.py defaults
    _N_RH_LOW   = 20.0;  _N_RH_HIGH   = 90.0
    _N_TF_LOW   = 33.0;  _N_TF_HIGH   = 120.0
    _N_P_HIGH   = 100000

    # Read alert state written by alerts.py (if it exists)
    _alert_state = {}
    _alert_state_path = os.path.join(DATA_DIR, 'alert_state.json')
    if os.path.exists(_alert_state_path):
        try:
            with open(_alert_state_path) as _af:
                _alert_state = json.load(_af)
        except Exception:
            pass

    _notif_rows = []

    # 1. Last sample time — warn if stale >1 hr, alert if very stale >6 hr
    _last_ts_str = get_real_ts(_latest_rec) if _latest_rec else None
    if _last_ts_str:
        try:
            _last_dt   = datetime.fromisoformat(_last_ts_str.replace(' ', 'T'))
            _ago_s     = int((datetime.now() - _last_dt).total_seconds())
            _ago_label = (f'{_ago_s // 60} min ago' if _ago_s < 3600
                          else f'{_ago_s // 3600} hr {(_ago_s % 3600) // 60} min ago' if _ago_s < 86400
                          else f'{_ago_s // 86400} day(s) ago')
            _stale_lvl = 'alert' if _ago_s > 21600 else 'warn' if _ago_s > 3600 else 'info'
            _notif_rows.append((_stale_lvl,
                f'● Last sample: {_last_ts_str} ({_ago_label})'))
        except Exception:
            _notif_rows.append(('info', f'● Last sample: {_last_ts_str}'))
    else:
        _notif_rows.append(('alert', '▲ Last sample: unknown — no data received'))

    # 2. ISO classification — red if ISO >= 8, yellow if ISO 7, green if <= 6
    if _iso_class is not None:
        if _iso_class <= 6:
            _notif_rows.append(('ok',
                f'● ISO class: ISO {_iso_class} — within target'))
        elif _iso_class == 7:
            _notif_rows.append(('warn',
                f'▲ ISO class: ISO {_iso_class} — marginal (target: ISO 6)'))
        else:
            _notif_rows.append(('alert',
                f'▲ ISO class: ISO {_iso_class} — EXCEEDS ISO 7 limit'))
    else:
        _notif_rows.append(('alert',
            '▲ ISO class: unclassifiable — particle count off-scale'))

    # 3. >=0.5 um concentration vs ISO 6 limit (35,200 /m3)
    _p05_now = sf(_latest_rec.get('ch2_diff_m3')) if _latest_rec else None
    if _p05_now is not None:
        if _p05_now > 352000:
            _notif_rows.append(('alert',
                f'▲ ≥0.5µm {_p05_now:,.0f}/m³ — exceeds ISO 7 (352 000)'))
        elif _p05_now > 35200:
            _notif_rows.append(('warn',
                f'▲ ≥0.5µm {_p05_now:,.0f}/m³ — exceeds ISO 6 (35 200)'))
        else:
            _notif_rows.append(('ok',
                f'● ≥0.5µm {_p05_now:,.0f}/m³ — within ISO 6'))
    else:
        _notif_rows.append(('mute', '○ ≥0.5µm: no data'))

    # 4. >=0.3 um concentration vs ISO 6 limit (102,000 /m3)
    _p_now = sf(_latest_rec.get('ch1_diff_m3')) if _latest_rec else None
    if _p_now is not None:
        if _p_now > _N_P_HIGH:
            _notif_rows.append(('alert',
                f'▲ ≥0.3µm {_p_now:,.0f}/m³ — above contamination threshold'))
        elif _p_now > 102000:
            _notif_rows.append(('warn',
                f'▲ ≥0.3µm {_p_now:,.0f}/m³ — exceeds ISO 6 (102 000)'))
        else:
            _notif_rows.append(('ok',
                f'● ≥0.3µm {_p_now:,.0f}/m³ — within ISO 6'))
    else:
        _notif_rows.append(('mute', '○ ≥0.3µm: no data'))

    # 5. Sensor health: laser and flow
    for _sn, _sok in [('Laser', laser_ok), ('Flow sensor', flow_ok)]:
        if _sok is True:
            _notif_rows.append(('ok',    f'● {_sn}: OK'))
        elif _sok is False:
            _notif_rows.append(('alert', f'▲ {_sn}: FAULT — check instrument'))
        else:
            _notif_rows.append(('mute',  f'○ {_sn}: no data'))

    # 6. Relative humidity
    _rh_now = sf(_latest_rec.get('RH_pct')) if _latest_rec else None
    if _rh_now is not None:
        if _rh_now < _N_RH_LOW:
            _notif_rows.append(('alert',
                f'▲ RH {_rh_now:.0f}% — below {_N_RH_LOW:.0f}% (static discharge risk)'))
        elif _rh_now > _N_RH_HIGH:
            _notif_rows.append(('alert',
                f'▲ RH {_rh_now:.0f}% — above {_N_RH_HIGH:.0f}% (condensation risk)'))
        else:
            _notif_rows.append(('ok',
                f'● RH {_rh_now:.0f}% — nominal'))
    else:
        _notif_rows.append(('mute', '○ RH: no sensor data'))

    # 7. Temperature
    _tc_now = sf(_latest_rec.get('temp_C')) if _latest_rec else None
    if _tc_now is not None and _tc_now > 0:
        _tf_now = round(_tc_now * 9/5 + 32, 1)
        if _tf_now < _N_TF_LOW:
            _notif_rows.append(('alert',
                f'▲ Temp {_tc_now:.1f}°C / {_tf_now:.0f}°F — below {_N_TF_LOW:.0f}°F threshold'))
        elif _tf_now > _N_TF_HIGH:
            _notif_rows.append(('alert',
                f'▲ Temp {_tc_now:.1f}°C / {_tf_now:.0f}°F — above {_N_TF_HIGH:.0f}°F threshold'))
        else:
            _notif_rows.append(('ok',
                f'● Temp {_tc_now:.1f}°C / {_tf_now:.0f}°F — nominal'))
    else:
        _notif_rows.append(('mute', '○ Temp: no sensor data'))

    # 8. Recent emails from alert_state.json (sent within last 24 hr)
    _alert_labels = {
        'rh_low':          'Low humidity',
        'rh_high':         'High humidity',
        'temp_low':        'Low temperature',
        'temp_high':       'High temperature',
        'particle_high':   'High particle count',
        'counter_offline': 'Counter offline',
    }
    _email_cutoff = datetime.now() - timedelta(hours=24)
    _email_sent   = False
    for _ak, _ats in _alert_state.items():
        try:
            _adt = datetime.fromisoformat(_ats)
            if _adt >= _email_cutoff:
                _notif_rows.append(('email',
                    f'✉ Email sent: {_alert_labels.get(_ak, _ak)} '
                    f'at {_adt.strftime("%H:%M")}'))
                _email_sent = True
        except Exception:
            pass
    if not _email_sent:
        _notif_rows.append(('mute', '○ No alerts emailed in last 24 hr'))

    # Build HTML rows
    _css_map = {'ok': 'ni-ok', 'warn': 'ni-warn', 'alert': 'ni-alert',
                'email': 'ni-email', 'info': 'ni-info', 'mute': 'ni-mute'}
    _has_alert = any(s in ('alert', 'warn') for s, _ in _notif_rows)
    _dot_html  = ' <span style="color:#f87171">&#9679;</span>' if _has_alert else \
                 ' <span style="color:#4ade80">&#9679;</span>'
    notif_panel_html = (
        '<div class="notif-wrap">'
        f'<button class="notif-btn" onclick="document.getElementById(\'notif-drop\').classList.toggle(\'open\')">'
        f'\u2299\u00a0ALERTS{_dot_html}</button>'
        '<div class="notif-drop" id="notif-drop">'
        '<div class="notif-hdr">\u2299\u00a0SYSTEM\u00a0STATUS</div>'
        + ''.join(
            f'<div class="notif-row {_css_map.get(s, "ni-mute")}">{msg}</div>'
            for s, msg in _notif_rows)
        + '</div></div>'
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
<title>Wright Lab &#8212; DUNE High Bay Clean Room Monitoring</title>
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
  .stats-strip {{
    display: flex; gap: 0; flex-wrap: wrap;
    background: #0a1628; border: 1px solid #1e3a5f;
    border-radius: 7px; padding: 9px 18px;
    margin-bottom: 14px; font-size: 11px;
  }}
  .stat-item {{ flex: 1; min-width: 160px; padding: 3px 12px 3px 0; }}
  .stat-k {{ color: #4b7ab8; text-transform: uppercase; letter-spacing: 0.8px; display: block; font-size: 9px; }}
  .stat-v {{ color: #93c5fd; font-weight: bold; font-size: 12px; }}
  .stat-v.warn {{ color: #fbbf24; }}
  .stat-v.alert {{ color: #f87171; }}
  .notif-wrap {{ position: relative; align-self: flex-end; margin-bottom: 6px; }}
  .notif-btn {{
    background: #060d1a; border: 1px solid #1e3a5f; border-radius: 6px;
    color: #4b7ab8; font-family: inherit; font-size: 11px; letter-spacing: 1.5px;
    text-transform: uppercase; padding: 5px 14px; cursor: pointer; white-space: nowrap;
  }}
  .notif-btn:hover {{ border-color: #38bdf8; color: #93c5fd; }}
  .notif-drop {{
    display: none; position: absolute; right: 0; top: calc(100% + 6px);
    min-width: 340px; max-width: 440px;
    max-height: 340px; overflow-y: auto;
    background: #060d1a; border: 1px solid #1e3a5f; border-radius: 6px;
    padding: 10px 16px 14px; z-index: 100;
    flex-direction: column; gap: 5px;
  }}
  .notif-drop.open {{ display: flex; }}
  .notif-hdr {{
    color: #4b7ab8; font-size: 10px; text-transform: uppercase;
    letter-spacing: 1.8px; padding-bottom: 6px;
    border-bottom: 1px solid #1e293b; margin-bottom: 4px;
  }}
  .notif-row {{ font-size: 13px; line-height: 1.7; }}
  .ni-ok    {{ color: #4ade80; }}
  .ni-warn  {{ color: #fbbf24; }}
  .ni-alert {{ color: #f87171; }}
  .ni-email {{ color: #93c5fd; }}
  .ni-info  {{ color: #d1d5db; }}
  .ni-mute  {{ color: #4b5563; }}
</style>
</head>
<body>

<div class="header">
  <h1>WRIGHT LAB &#8212; DUNE HIGH BAY CLEAN ROOM MONITORING</h1>
  <div class="sub">Particle Plus Model 7301 &nbsp;&middot;&nbsp; Real-time Particulate &amp; Environmental Dashboard</div>
</div>

{conn_banner}

<div class="controls">
  <div class="ctrl-group">
    <label>Time Range</label>
    <select id="sel-range" onchange="filterAndRender()">
      <option value="0">All data (30 days)</option>
      <option value="30">Last 30 min</option>
      <option value="60">Last 1 hr</option>
      <option value="120">Last 2 hr</option>
      <option value="180">Last 3 hr</option>
      <option value="360">Last 6 hr</option>
      <option value="720">Last 12 hr</option>
      <option value="1440" selected>Last 24 hr</option>
      <option value="2880">Last 2 days</option>
      <option value="10080">Last 7 days</option>
    </select>
  </div>
  <div class="updated">Last pushed: {updated}</div>
  <div style="flex:1"></div>
  {notif_panel_html}
  {iso_badge_html}
</div>

<div class="status-strip">{status_strip_html}</div>
<div class="cards">{env_cards_html}</div>

<div class="stats-strip">
  <div class="stat-item"><span class="stat-k">Samples in window</span><span class="stat-v" id="stat-n">--</span></div>
  <div class="stat-item"><span class="stat-k">0.3 &micro;m &mdash; mean</span><span class="stat-v" id="stat-mean1">--</span></div>
  <div class="stat-item"><span class="stat-k">0.3 &micro;m &mdash; peak</span><span class="stat-v" id="stat-peak1">--</span></div>
  <div class="stat-item"><span class="stat-k">ISO 7 exceedances &nbsp;(0.5 &micro;m &gt; 352k /m&sup3;)</span><span class="stat-v" id="stat-exc7">--</span></div>
  <div class="stat-item"><span class="stat-k">Offline gaps detected</span><span class="stat-v" id="stat-gaps">--</span></div>
</div>

<div class="chart-panel">
  <div class="chart-title">Particle Concentration Over Time &nbsp;&#8212; all 6 size channels (log scale, counts / m&#179;, ISO 14644-1 reference lines shown for 0.5 &micro;m)</div>
  <div id="chart-counts" style="height:360px"></div>
</div>

<div class="chart-panel">
  <div class="chart-title">PM Mass Concentration Over Time &nbsp;(&#956;g / m&#179;)</div>
  <div id="chart-pm" style="height:300px"></div>
</div>

<div class="row2">
  <div class="chart-panel">
    <div class="chart-title">Latest Particle Size Distribution &nbsp;(most recent sample &mdash; log scale)</div>
    <div id="chart-dist" style="height:280px"></div>
  </div>
  <div class="chart-panel">
    <div class="chart-title">Temperature &amp; Humidity Over Time</div>
    <div id="chart-env" style="height:280px"></div>
  </div>
</div>

<script>
const TS       = {ts_js};
const COUNTS   = {counts_traces_js};
const PM       = {pm_traces_js};
const DIST     = {dist_traces_js};
const CH1_C    = {ch1_counts_js};
const CH2_PM   = {ch2_pm_js};
const LIVE_TS  = {live_ts_js};
const TEMP_F   = {temp_f_js};
const RH_VALS  = {rh_js};
const ISO_LINES = {iso_lines_js};

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

// Returns vrect shapes for gaps > GAP_THRESH_MS in a timestamp array.
// These are rendered as subtle grey bands indicating the counter was offline.
function gapShapes(ts) {{
  const GAP_THRESH_MS = 90 * 60 * 1000;   // 90 minutes
  const shapes = [];
  for (let k = 1; k < ts.length; k++) {{
    if (new Date(ts[k]) - new Date(ts[k-1]) > GAP_THRESH_MS) {{
      shapes.push({{
        type: 'rect', xref: 'x', yref: 'paper',
        x0: ts[k-1], x1: ts[k], y0: 0, y1: 1,
        fillcolor: 'rgba(100,116,139,0.10)', line: {{ width: 0 }}, layer: 'below'
      }});
    }}
  }}
  return shapes;
}}

// ISO 14644-1 reference lines for the 0.5 µm channel (horizontal dashed lines).
function isoShapes() {{
  return ISO_LINES.map(l => ({{
    type: 'line', xref: 'paper', x0: 0, x1: 1,
    yref: 'y', y0: l.y, y1: l.y,
    line: {{ color: l.color, width: l.width, dash: l.dash }}
  }}));
}}
function isoAnnotations() {{
  return ISO_LINES.map(l => ({{
    xref: 'paper', x: 1.02, yref: 'y', y: l.y,
    text: l.bold ? '<b>' + l.label + '</b>' : l.label,
    showarrow: false, xanchor: 'left',
    font: {{ color: l.color, size: l.bold ? 12 : 10, family: 'Courier New, monospace' }}
  }}));
}}

function updateStats(i) {{
  const ts   = TS.slice(i);
  const ch1  = COUNTS[0].y.slice(i).filter(v => v !== null && v !== undefined);
  const ch2  = COUNTS[1].y.slice(i).filter(v => v !== null && v !== undefined);
  const n    = ts.length;
  const fmt  = v => (v !== null && !isNaN(v)) ? Math.round(v).toLocaleString() + '\u00a0/m\u00b3' : '--';
  const mean1 = ch1.length ? ch1.reduce((a,b)=>a+b,0)/ch1.length : null;
  const peak1 = ch1.length ? Math.max(...ch1) : null;
  const exc7  = ch2.filter(v => v > 352000).length;
  const exc7s = ch2.length ? exc7 + '\u00a0/\u00a0' + ch2.length + '\u00a0(' + (exc7/ch2.length*100).toFixed(0) + '%)' : '--';
  const gaps  = gapShapes(ts).length;

  document.getElementById('stat-n').textContent     = n;
  document.getElementById('stat-mean1').textContent = fmt(mean1);
  document.getElementById('stat-peak1').textContent = fmt(peak1);

  const excEl = document.getElementById('stat-exc7');
  excEl.textContent  = exc7s;
  excEl.className    = 'stat-v' + (exc7 > 0 ? ' warn' : '');

  const gapEl = document.getElementById('stat-gaps');
  gapEl.textContent = gaps > 0 ? gaps + (gaps === 1 ? ' gap' : ' gaps') : 'none';
  gapEl.className   = 'stat-v' + (gaps > 0 ? ' warn' : '');
}}

function filterAndRender() {{
  const mins = parseInt(document.getElementById('sel-range').value);
  const i    = sliceIdx(mins);
  const ts   = TS.slice(i);
  const gaps = gapShapes(ts);

  Plotly.react('chart-counts', sliceTraces(COUNTS, i),
    Object.assign({{}}, DARK, {{
      yaxis:       Object.assign({{}}, DARK.yaxis, {{ title: 'Counts / m\u00b3', type: 'log' }}),
      xaxis:       Object.assign({{}}, DARK.xaxis, {{ title: '' }}),
      margin:      {{ l: 60, r: 72, t: 30, b: 50 }},
      shapes:      [...gaps, ...isoShapes()],
      annotations: isoAnnotations(),
    }}), {{responsive: true, displaylogo: false}});

  Plotly.react('chart-pm', sliceTraces(PM, i),
    Object.assign({{}}, DARK, {{
      yaxis:  Object.assign({{}}, DARK.yaxis, {{ title: '\u03bcg / m\u00b3' }}),
      xaxis:  Object.assign({{}}, DARK.xaxis, {{ title: '' }}),
      shapes: gaps,
    }}), {{responsive: true, displaylogo: false}});

  // Dynamic log-scale range: expand to the actual max count so tall bars are never clipped
  const _distMax = (DIST[0] && DIST[0].y.length) ? Math.max(...DIST[0].y) : 100;
  const _distLogMax = Math.log10(Math.max(_distMax, 1)) + 0.3;
  Plotly.react('chart-dist', DIST,
    Object.assign({{}}, DARK, {{
      showlegend: false, bargap: 0.3,
      yaxis: Object.assign({{}}, DARK.yaxis, {{ title: 'Counts / m\u00b3', type: 'log', range: [-0.5, _distLogMax] }}),
      xaxis: Object.assign({{}}, DARK.xaxis, {{ title: 'Particle Size (\u03bcm)' }}),
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
    margin: {{ l: 60, r: 70, t: 30, b: 50 }},
    xaxis:  Object.assign({{}}, DARK.xaxis,  {{ title: '' }}),
    yaxis:  Object.assign({{}}, DARK.yaxis,  {{ title: 'Temperature (\u00b0F)' }}),
    yaxis2: {{ title: {{ text: 'Humidity (%)', standoff: 15 }},
               overlaying: 'y', side: 'right',
               gridcolor: '#1e293b', linecolor: '#334155',
               tickfont: {{ color: '#6b7280', size: 10 }},
               title_font: {{ color: '#6b7280', size: 11 }} }},
  }}), {{responsive: true, displaylogo: false}});

  updateStats(i);
}}

filterAndRender();

document.addEventListener('click', function(e) {{
  var drop = document.getElementById('notif-drop');
  if (drop && drop.classList.contains('open') && !drop.parentElement.contains(e.target)) {{
    drop.classList.remove('open');
  }}
}});
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
    csv_dest  = os.path.join(repo_dir, 'data', 'live.csv')

    os.makedirs(os.path.join(repo_dir, 'data'), exist_ok=True)

    # generate fresh dashboard
    generate_dashboard_html(csv_path, html_path)

    # copy latest CSV into repo (skip if same file or not yet created)
    if os.path.exists(csv_path) and not os.path.samefile(csv_path, csv_dest):
        shutil.copy2(csv_path, csv_dest)
        log(f"Copied CSV → {csv_dest}")
    elif not os.path.exists(csv_path):
        log(f"CSV not yet created, skipping copy")

    # git add + commit + push
    # Only stage known data-output files — never source code or untracked files.
    # Build list dynamically so a missing file (e.g. live.csv before first sync)
    # doesn't cause git add to fail.
    _data_files = [
        'data/live.csv',
        'data/env_live.csv',
        'data/session_baseline.txt',
        'index.html',
    ]
    _to_stage = [f for f in _data_files
                 if os.path.exists(os.path.join(repo_dir, f))]
    if not _to_stage:
        log("No data output files to stage — skipping push")
        return True

    cmds = [
        ['git', '-C', repo_dir, 'add'] + _to_stage,
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
        sync_counter_clock(client)   # set RTC so new records get real timestamps
        total = get_record_count(client)
        log(f"Records on counter: {total}")

        # ── determine last synced record ──────────────────────────────────────
        # Prefer counter_state.json (written after every sync + after every erase).
        # Fall back to scanning the archive CSV only on first run before state file exists.
        from features.data_manager import (get_last_synced, set_last_synced,
                                            rebuild_live_csv)
        if os.path.exists(COUNTER_STATE):
            last_saved = get_last_synced(COUNTER_STATE)
        else:
            last_saved = 0
            if os.path.exists(ARCHIVE_CSV):
                with open(ARCHIVE_CSV, 'r') as f:
                    for row in csv.DictReader(f):
                        try:
                            n = int(float(row.get('record_number', 0) or 0))
                            if n > last_saved:
                                last_saved = n
                        except (ValueError, TypeError):
                            pass

        # ── detect counter erase / reset ─────────────────────────────────────
        # If the counter has fewer records than our last synced number, the counter
        # was erased and its record numbers restarted from 1.  Reset to sync from 1.
        if last_saved > total:
            log(f"Counter reset detected: last_synced={last_saved} but counter_total={total}. "
                f"Restarting sync from record 1.", 'WARN')
            last_saved = 0

        if last_saved >= total:
            log(f"Already up to date (synced to record {last_saved}, counter has {total})")
            return True

        start = last_saved + 1
        n_new = total - last_saved
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
                    _ts_ok = data.get('timestamp_valid', None)
                    log(f"  [{i:4d}/{total}] (new {i-last_saved}/{n_new}) "
                        f"Date: {data.get('date','') or '(empty)'}  "
                        f"Time: {data.get('time','') or '(empty)'}  "
                        f"ts_valid={_ts_ok}  "
                        f"temp={data.get('temp_C','?')}C  "
                        f"RH={data.get('RH_pct','?')}%  "
                        f"ch1={data.get('ch1_diff_m3','?')}/m³")
                else:
                    failed.append(i)
            except Exception as e:
                log(f"  [{i:4d}/{total}] Error: {e}", 'ERROR')
                failed.append(i)

        # ── save to archive (never trimmed) ──────────────────────────────────
        saved = save_to_csv(records, ARCHIVE_CSV)

        if failed:
            log(f"WARNING: {len(failed)} failed — NOT erasing", 'WARN')
            return False

        # ── update sync state and rebuild 30-day live.csv ────────────────────
        if saved:
            set_last_synced(COUNTER_STATE, total)
            n_live = rebuild_live_csv(ARCHIVE_CSV, LIVE_CSV)
            log(f"live.csv rebuilt: {n_live} records (last 30 days)")

        # ── erase counter if needed ───────────────────────────────────────────
        if saved and (ERASE_AFTER_SYNC or total > TRIM_CAP):
            if total > TRIM_CAP:
                log(f"Counter at {total} records (>{TRIM_CAP}) — auto-erasing to free storage")
            erase_counter(client)

        return True

    finally:
        if own_client:
            client.close()


def mode_live():
    """
    Continuously snapshot live (in-progress) data to ENV_SNAPSHOT_CSV.
    Useful for watching what the counter is currently seeing (temp/RH).
    Trims the snapshot file to the last 30 days every 6 hours.
    """
    log("MODE: --live  (streaming live snapshots every 10s)")
    log(f"  Output: {ENV_SNAPSHOT_CSV}")
    log("  Ctrl+C to stop")

    from features.data_manager import trim_env_csv
    _last_trim = time.time()

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
                    save_to_csv([data], ENV_SNAPSHOT_CSV)
                    log(f"Live: "
                        f"Date: {data.get('date','') or '(empty)'}  "
                        f"Time: {data.get('time','') or '(empty)'}  "
                        f"ts_valid={data.get('timestamp_valid',None)}  "
                        f"temp={data.get('temp_C')}C  "
                        f"RH={data.get('RH_pct')}%  "
                        f"ch1={data.get('ch1_diff_m3')}/m³")
            except Exception as e:
                log(f"Live error: {e}", 'ERROR')
            finally:
                client.close()
        finally:
            _modbus_lock.release()

        # trim env snapshots to 30 days every 6 hours
        if time.time() - _last_trim > 21600:
            n_kept = trim_env_csv(ENV_SNAPSHOT_CSV)
            log(f"Trimmed env_live.csv to 30 days: {n_kept} rows kept")
            _last_trim = time.time()

        time.sleep(10)


def mode_trim():
    """Flush new records to archive then erase counter if above TRIM_CAP.

    Delegates entirely to mode_sync(), which already handles:
      • counter_state.json-based last-synced tracking
      • post-erase counter-reset detection
      • auto-erase when total > TRIM_CAP
    trim_counter.py is kept as a standalone emergency tool only.
    """
    log(f"MODE: --trim  (cap={TRIM_CAP})")
    return mode_sync()


def mode_dashboard():
    """Generate HTML and push to GitHub Pages"""
    log("MODE: --dashboard")
    push_to_github(GITHUB_REPO_DIR, LIVE_CSV)


def mode_all():
    """
    Run sampling + live streaming + dashboard updates together.
    Recommended for the tmux session on noether.
    """
    import threading
    from features.data_manager import migrate_old_files, rebuild_live_csv, trim_env_csv

    log("MODE: --all  (sample + live + dashboard)")

    # ── one-time migration from legacy file names ─────────────────────────────
    migrate_old_files(DATA_DIR)
    log("Data file migration check complete")

    # ── rebuild live.csv from archive at startup ──────────────────────────────
    if os.path.exists(ARCHIVE_CSV):
        n_live = rebuild_live_csv(ARCHIVE_CSV, LIVE_CSV)
        log(f"Startup: live.csv rebuilt with {n_live} records (last 30 days)")
    trim_env_csv(ENV_SNAPSHOT_CSV)

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
    parser.add_argument('--trim',      action='store_true',
                        help=f'Flush + erase counter if records > TRIM_CAP ({TRIM_CAP})')
    args = parser.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

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
    elif args.trim:
        mode_trim()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
