#!/usr/bin/env python3
"""
trim_counter.py — Emergency storage-cap manager for the Particle Plus 7000.

The device stores up to ~45,000 records in a circular buffer.  When the
network is lost the counter keeps recording; if the buffer fills the oldest
records are silently overwritten.  This script keeps the on-device count
below CAP by flushing new records to the archive CSV and then erasing.

  Run manually:    python3 trim_counter.py
  Check only:      python3 trim_counter.py --check
  Force flush+erase regardless of count: python3 trim_counter.py --force

IMPORTANT — field names, rounding, and register map in read_latched_record()
are kept byte-for-byte identical to particle_plus.py::read_latched_record()
so that records from both scripts can safely share measurement_archive.csv.

Counter-state (last synced record) is tracked via data/counter_state.json,
the same file that particle_plus.py::mode_sync writes.  After an erase the
state is reset to 0 so the next sync (by either script) starts from record 1.
"""

import argparse
import csv
import json
import os
import struct
import time
from datetime import datetime

from pymodbus.client import ModbusTcpClient


# ─── CONFIG ───────────────────────────────────────────────────────────────────

COUNTER_IP    = '10.66.66.68'
PORT          = 502

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(BASE_DIR, 'data')
OUTPUT_CSV    = os.path.join(DATA_DIR, 'measurement_archive.csv')
LIVE_CSV      = os.path.join(DATA_DIR, 'live.csv')
COUNTER_STATE = os.path.join(DATA_DIR, 'counter_state.json')

CAP = 10_000   # erase threshold; device max ~45,000

# ──────────────────────────────────────────────────────────────────────────────


def log(msg, level='INFO'):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line)


# ─── COUNTER-STATE (counter_state.json) ───────────────────────────────────────
# Mirrors features/data_manager.py exactly so both scripts share the same file.

def get_last_synced(state_path):
    """Return last synced record number from counter_state.json, or 0 if absent/corrupt."""
    if not os.path.exists(state_path):
        return 0
    try:
        with open(state_path) as f:
            return int(json.load(f).get('last_synced', 0))
    except (json.JSONDecodeError, ValueError, KeyError, OSError):
        return 0


def set_last_synced(state_path, n):
    """Persist last synced record number to counter_state.json."""
    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except Exception:
            pass
    state['last_synced'] = n
    state['updated'] = datetime.now().isoformat()
    with open(state_path, 'w') as f:
        json.dump(state, f)


def reset_sync_state(state_path):
    """Reset last_synced to 0 after a counter erase so the next sync starts from record 1."""
    with open(state_path, 'w') as f:
        json.dump({'last_synced': 0, 'erased': datetime.now().isoformat()}, f)


# ─── DECODERS — identical to particle_plus.py ─────────────────────────────────
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


def encode_i32(value):
    unsigned = value & 0xFFFFFFFF
    return [unsigned & 0xFFFF, (unsigned >> 16) & 0xFFFF]


def decode_string(registers):
    # Device stores one ASCII character per register in the LOW byte;
    # the high byte is always 0x00.  Match particle_plus.py exactly.
    result = ''
    for reg in registers:
        low = reg & 0xFF
        if low == 0:
            break
        result += chr(low)
    return result.strip()


# ─── MODBUS HELPERS ───────────────────────────────────────────────────────────

def connect():
    client = ModbusTcpClient(COUNTER_IP, port=PORT, timeout=5)
    if not client.connect():
        log(f"Connection to {COUNTER_IP}:{PORT} failed", 'ERROR')
        return None
    log(f"Connected to {COUNTER_IP}:{PORT}")
    return client


def get_record_count(client):
    r = client.read_holding_registers(address=8000, count=2)
    if r.isError():
        return 0
    return decode_u32(r.registers)


def latch_record(client, record_number):
    client.write_registers(address=8002, values=encode_i32(record_number))
    time.sleep(0.3)


def read_latched_record(client):
    """
    Read all fields from the currently-latched record.

    Field names, register addresses, and rounding are kept IDENTICAL to
    particle_plus.py::read_latched_record() so records from both scripts
    are schema-compatible in measurement_archive.csv.

    Returns a dict on success, or None if the record is invalid.
    """
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

    # Status bits — field names match particle_plus.py
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

    # 6 particle channels — register map and rounding match particle_plus.py
    for i in range(6):
        offset = i * 2
        ch = f'ch{i+1}'

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

    data['sync_time'] = datetime.now().isoformat()
    return data


# ─── CSV ──────────────────────────────────────────────────────────────────────

def save_to_csv(records, csv_path):
    """Append records to CSV, writing header only if the file is new."""
    if not records:
        return 0
    file_exists = os.path.exists(csv_path)
    with open(csv_path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(records)
    return len(records)


# ─── CORE LOGIC ───────────────────────────────────────────────────────────────

def flush_new_records(client):
    """
    Sync only records not yet saved to the archive CSV.

    Uses counter_state.json (same file as particle_plus.py::mode_sync) to
    determine the last synced record.  Includes post-erase detection:
    if last_synced > counter_total the counter was erased and restarted,
    so we reset last_synced to 0 and sync from record 1.

    Returns (n_saved, had_failures).
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    total      = get_record_count(client)
    last_saved = get_last_synced(COUNTER_STATE)
    log(f"Counter: {total} records total; last synced: {last_saved}")

    # Post-erase detection — mirrors particle_plus.py::mode_sync
    if last_saved > total:
        log(f"Counter reset detected: last_synced={last_saved} but "
            f"counter_total={total}. Restarting sync from record 1.", 'WARN')
        last_saved = 0

    if last_saved >= total:
        log("Already up to date — nothing to flush")
        return 0, False

    start = last_saved + 1
    n_new = total - last_saved
    log(f"Flushing {n_new} new records ({start}–{total}) → {OUTPUT_CSV}")

    records  = []
    failures = []

    for i in range(start, total + 1):
        try:
            latch_record(client, i)
            data = read_latched_record(client)
            if data:
                records.append(data)
                log(f"  [{i:4d}/{total}] date={data.get('date','') or '(empty)'}  "
                    f"time={data.get('time','') or '(empty)'}  "
                    f"temp={data.get('temp_C','?')}C  "
                    f"ch1={data.get('ch1_diff_m3','?')}/m³")
            else:
                failures.append(i)
                log(f"  [{i:4d}/{total}] empty/invalid — skipped", 'WARN')
        except Exception as exc:
            failures.append(i)
            log(f"  [{i:4d}/{total}] error: {exc}", 'ERROR')

    n_saved = save_to_csv(records, OUTPUT_CSV)
    log(f"Flush complete: {n_saved} records saved, {len(failures)} failures")
    if failures:
        log(f"Failed record numbers: {failures}", 'WARN')

    if n_saved > 0 and not failures:
        set_last_synced(COUNTER_STATE, total)
        log(f"counter_state.json updated: last_synced={total}")

        # Rebuild live.csv (30-day window) from the updated archive
        try:
            from features.data_manager import rebuild_live_csv
            n_live = rebuild_live_csv(OUTPUT_CSV, LIVE_CSV)
            log(f"live.csv rebuilt: {n_live} records (last 30 days)")
        except Exception as e:
            log(f"Could not rebuild live.csv: {e}", 'WARN')

    return n_saved, bool(failures)


def erase_counter(client):
    """Write the erase magic value and reset sync state. Returns True if counter reaches 0."""
    log("Erasing counter memory (reg 8004 ← 0x9559)...")
    client.write_registers(address=8004, values=[0x9559])
    time.sleep(3)
    remaining = get_record_count(client)
    log(f"Records remaining after erase: {remaining}")
    if remaining == 0:
        reset_sync_state(COUNTER_STATE)
        log("counter_state.json reset to last_synced=0 after erase")
    return remaining == 0


def trim_if_full(cap=CAP, force=False):
    """
    Main entry point for standalone use.

    If record count > cap (or force=True): flush new records then erase.
    Never erases if flush had any failures.
    Returns True on success or if no action was needed.
    """
    client = connect()
    if client is None:
        return False

    try:
        count = get_record_count(client)
        log(f"Counter has {count} records  (cap={cap})")

        if not force and count <= cap:
            log("Below cap — no trim needed")
            return True

        if force:
            log("--force: flushing and erasing regardless of count")
        else:
            log(f"{count} > {cap} — flushing then erasing")

        n_saved, had_failures = flush_new_records(client)

        if had_failures:
            log("Skipping erase: flush had failures — re-run to retry", 'WARN')
            return False

        ok = erase_counter(client)
        if ok:
            log(f"Trim complete: flushed {n_saved} new records, counter reset to 0")
        else:
            log("Erase command sent but counter may not be fully cleared", 'WARN')
        return ok

    finally:
        client.close()


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Trim Particle Plus 7000 storage: flush new records then erase if above cap')
    parser.add_argument('--check', action='store_true',
                        help='Report record count only — no flush or erase')
    parser.add_argument('--force', action='store_true',
                        help=f'Flush + erase regardless of count (ignores cap={CAP})')
    parser.add_argument('--cap', type=int, default=CAP,
                        help=f'Override cap threshold (default: {CAP})')
    args = parser.parse_args()

    print(f"trim_counter.py  |  target={COUNTER_IP}:{PORT}  |  cap={args.cap}")
    print(f"  archive → {OUTPUT_CSV}")
    print()

    if args.check:
        client = connect()
        if client:
            count = get_record_count(client)
            last  = get_last_synced(COUNTER_STATE)
            client.close()
            log(f"Record count: {count}  (cap={args.cap}, "
                f"{'OVER' if count > args.cap else 'OK'})  "
                f"last_synced={last}")
        return

    trim_if_full(cap=args.cap, force=args.force)


if __name__ == '__main__':
    main()
