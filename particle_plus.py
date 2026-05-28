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
import socket
import subprocess
from datetime import datetime, timedelta

from pymodbus.client import ModbusTcpClient

# ─── CONFIG ───────────────────────────────────────────────────────────────────

COUNTER_IP   = '10.66.114.55'
PORT         = 502

BASE_DIR     = '/home/rraut/particle_plus'
OUTPUT_CSV   = f'{BASE_DIR}/particle_data_archive.csv'
LIVE_CSV     = f'{BASE_DIR}/particle_data_live.csv'
LOG_FILE     = f'{BASE_DIR}/sync_log.txt'

# sampling schedule
SAMPLE_TIME_S       = 60      # 1 minute sample
HOLD_TIME_S         = 1800    # 30 min between samples = twice per hour
DELAY_TIME_S        = 5       # pump stabilization
CYCLES              = 1       # 1 sample per cycle then hold

# sync/erase
ERASE_AFTER_SYNC    = False   # set True after verifying data
MIN_RECORDS_TO_SYNC = 1

# github
GITHUB_REPO_DIR     = f'{BASE_DIR}/dashboard'   # local clone of your repo
GITHUB_BRANCH       = 'main'
GITHUB_REMOTE       = 'origin'

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
    Read last 24hrs of CSV data and generate a self-contained
    HTML file with Plotly charts. No server needed — pure static HTML.
    """
    # read CSV
    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

    # filter last 24 hours
    cutoff = datetime.now() - timedelta(hours=24)
    recent = []
    for row in rows:
        try:
            dt_str = f"{row.get('date','')} {row.get('time','')}"
            dt = datetime.strptime(dt_str.strip(), '%Y-%m-%d %H:%M:%S')
            if dt >= cutoff:
                recent.append(row)
        except Exception:
            continue

    log(f"Dashboard: {len(recent)} records in last 24hrs")

    # build data arrays for javascript
    def safe_float(val):
        try:
            return float(val) if val not in (None, '', 'None') else 'null'
        except Exception:
            return 'null'

    timestamps = [f"{r.get('date','')} {r.get('time','')}" for r in recent]
    temp       = [safe_float(r.get('temp_C'))    for r in recent]
    rh         = [safe_float(r.get('RH_pct'))    for r in recent]

    channels = {}
    for ch in ['ch1','ch2','ch3','ch4','ch5','ch6']:
        channels[ch] = {
            'size'     : recent[0].get(f'{ch}_size_um', '?') if recent else '?',
            'diff_m3'  : [safe_float(r.get(f'{ch}_diff_m3'))  for r in recent],
            'sum_m3'   : [safe_float(r.get(f'{ch}_sum_m3'))   for r in recent],
        }

    # format as JS arrays
    ts_js = str(timestamps).replace("'", '"')

    ch_traces = ''
    for ch, vals in channels.items():
        ch_traces += f"""
        {{
            x: {ts_js},
            y: {vals['diff_m3']},
            name: '{ch} (≥{vals["size"]}µm) diff/m³',
            type: 'scatter',
            mode: 'lines+markers',
            line: {{width: 1.5}},
            marker: {{size: 4}}
        }},"""

    updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="1800">  <!-- auto-refresh every 30min -->
<title>Wright Lab — Particle Counter Dashboard</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  body {{
    font-family: 'Segoe UI', sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    margin: 0;
    padding: 20px;
  }}
  h1 {{
    color: #58a6ff;
    border-bottom: 1px solid #30363d;
    padding-bottom: 10px;
  }}
  .subtitle {{
    color: #8b949e;
    font-size: 0.9em;
    margin-top: -10px;
    margin-bottom: 20px;
  }}
  .card {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 15px;
    margin-bottom: 20px;
  }}
  .updated {{
    color: #8b949e;
    font-size: 0.8em;
    text-align: right;
  }}
  .status-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 20px;
  }}
  .stat {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 15px;
    text-align: center;
  }}
  .stat-value {{
    font-size: 2em;
    font-weight: bold;
    color: #58a6ff;
  }}
  .stat-label {{
    font-size: 0.8em;
    color: #8b949e;
    margin-top: 4px;
  }}
</style>
</head>
<body>

<h1>🔬 Wright Lab — Particle Counter</h1>
<p class="subtitle">
  Particles Plus 7000 Series | Last 24 hours | 
  Sampling every 30 minutes
</p>

<!-- Status cards -->
<div class="status-grid">
  <div class="stat">
    <div class="stat-value" id="last-temp">--</div>
    <div class="stat-label">Temperature (°C)</div>
  </div>
  <div class="stat">
    <div class="stat-value" id="last-rh">--</div>
    <div class="stat-label">Humidity (%)</div>
  </div>
  <div class="stat">
    <div class="stat-value">{len(recent)}</div>
    <div class="stat-label">Samples (24hr)</div>
  </div>
  <div class="stat">
    <div class="stat-value" id="last-ch1">--</div>
    <div class="stat-label">Latest ch1 /m³</div>
  </div>
</div>

<!-- Particle counts chart -->
<div class="card">
  <div id="plot-particles" style="height:450px;"></div>
</div>

<!-- Temperature + RH chart -->
<div class="card">
  <div id="plot-env" style="height:300px;"></div>
</div>

<p class="updated">Last updated: {updated} (UTC) — auto-refreshes every 30 min</p>

<script>
const timestamps = {ts_js};
const temp       = {temp};
const rh         = {rh};

// update stat cards with most recent values
if (temp.length > 0)  document.getElementById('last-temp').innerText =
    temp.filter(v => v !== null).slice(-1)[0] ?? '--';
if (rh.length > 0)    document.getElementById('last-rh').innerText =
    rh.filter(v => v !== null).slice(-1)[0] ?? '--';

// particle counts plot
const particleTraces = [{ch_traces}];

const particleLayout = {{
  title: 'Particle Counts (differential, per m³) — Last 24 Hours',
  paper_bgcolor: '#161b22',
  plot_bgcolor:  '#0d1117',
  font:   {{color: '#c9d1d9'}},
  xaxis:  {{gridcolor: '#30363d', title: 'Time'}},
  yaxis:  {{gridcolor: '#30363d', title: 'Particles / m³'}},
  legend: {{bgcolor: '#161b22', bordercolor: '#30363d', borderwidth: 1}},
  margin: {{t: 50, b: 80, l: 80, r: 20}}
}};

Plotly.newPlot('plot-particles', particleTraces, particleLayout,
               {{responsive: true}});

// update ch1 stat card
const ch1vals = particleTraces[0]?.y ?? [];
const lastCh1 = ch1vals.filter(v => v !== null).slice(-1)[0];
if (lastCh1 !== undefined)
  document.getElementById('last-ch1').innerText = lastCh1.toFixed(1);

// env plot
const envTraces = [
  {{
    x: timestamps, y: temp,
    name: 'Temperature (°C)',
    type: 'scatter', mode: 'lines+markers',
    line: {{color: '#ff7b72', width: 1.5}},
    marker: {{size: 4}},
    yaxis: 'y1'
  }},
  {{
    x: timestamps, y: rh,
    name: 'Humidity (%)',
    type: 'scatter', mode: 'lines+markers',
    line: {{color: '#79c0ff', width: 1.5}},
    marker: {{size: 4}},
    yaxis: 'y2'
  }}
];

const envLayout = {{
  title: 'Temperature & Humidity — Last 24 Hours',
  paper_bgcolor: '#161b22',
  plot_bgcolor:  '#0d1117',
  font:   {{color: '#c9d1d9'}},
  xaxis:  {{gridcolor: '#30363d'}},
  yaxis:  {{gridcolor: '#30363d', title: 'Temperature (°C)',
            titlefont: {{color: '#ff7b72'}}}},
  yaxis2: {{title: 'Humidity (%)', titlefont: {{color: '#79c0ff'}},
            overlaying: 'y', side: 'right', gridcolor: '#30363d'}},
  legend: {{bgcolor: '#161b22'}},
  margin: {{t: 50, b: 80, l: 80, r: 80}}
}};

Plotly.newPlot('plot-env', envTraces, envLayout, {{responsive: true}});
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

    # copy latest CSV into repo
    shutil.copy2(csv_path, csv_dest)
    log(f"Copied CSV → {csv_dest}")

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

    params_written = False

    while True:
        client = connect()
        if client is None:
            log(f"Retrying in 60s...")
            time.sleep(60)
            continue

        try:
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

        if total < MIN_RECORDS_TO_SYNC:
            log("Below sync threshold, skipping")
            return True

        records = []
        failed  = []

        for i in range(1, total + 1):
            try:
                latch_record(client, i)
                data = read_latched_record(client)
                if data:
                    records.append(data)
                    log(f"  [{i:4d}/{total}] "
                        f"{data.get('date','?')} {data.get('time','?')} | "
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

    t_live = threading.Thread(target=mode_live, daemon=True)
    t_live.start()

    # main thread runs the scheduler (includes dashboard push after each sync)
    mode_sample()


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
    args = parser.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)

    if args.sample:
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
