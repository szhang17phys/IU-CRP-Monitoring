#!/usr/bin/env python3
"""
flush_and_erase.py — one-shot tool to pull EVERY stored record off the
Particle Plus counter into the local archive CSV, then (optionally) erase
the counter's memory.

SAFETY MODEL — the counter is erased ONLY when every one of these passes:
  1. We can connect to the counter.
  2. We can read the record count, and it is > 0.
     (A failed/again read ABORTS — it is never silently treated as "empty".)
  3. EVERY record reads back successfully AND its latched record_number matches
     the record we asked for. Any failure → abort, nothing erased.
  4. The records are appended to the archive CSV, and the write is then
     VERIFIED by re-reading the file and confirming it grew by exactly the
     expected number of rows AND that those rows are the records we just read.
  5. Only then, and only if ERASE_AFTER_SYNC is True, is the counter erased,
     and the erase is itself verified (count returns to 0).

At any anomaly the tool aborts WITHOUT erasing — data is never lost.

NETWORK RESILIENCE: every modbus call is retried with exponential backoff and
an automatic reconnect between attempts (TIMEOUT / MODBUS_RETRIES / READ_RETRIES
below). This rides out a flaky link — but note that nothing can overcome heavy
packet loss (e.g. ~77% loss seen from a cluster login node). Run this from a
host with a stable path to the counter (e.g. noether / the cleanroom subnet).

This script REUSES the proven modbus / CSV / erase functions from
particle_plus.py, so the archive schema always matches the daemon's (no
column drift / corruption). particle_plus.py is imported, never modified.

Runs from any computer / working directory:
    python3 flush_and_erase.py
"""

import os
import sys
import csv
import time
from datetime import datetime

from pymodbus.client import ModbusTcpClient

# Reuse the daemon's proven implementation (same decoders, same CSV schema,
# same erase + sync-state reset). Python puts this script's directory on
# sys.path, so this import works regardless of where you launch from.
import particle_plus as pp

# ─── CONFIG ───────────────────────────────────────────────
COUNTER_IP       = pp.COUNTER_IP
PORT             = pp.PORT
OUTPUT_CSV       = pp.ARCHIVE_CSV     # same local-only archive the daemon writes
ERASE_AFTER_SYNC = False              # set True ONLY after confirming the data

# Network resilience knobs (tune up for a flaky link).
TIMEOUT          = 10   # seconds per modbus request (particle_plus default = 5)
MODBUS_RETRIES   = 5    # pymodbus internal retries per request
READ_RETRIES     = 5    # our app-level retries (reconnect + exponential backoff)
CONNECT_RETRIES  = 5    # attempts to (re)establish the TCP connection
BACKOFF_MAX_S    = 8.0  # cap on the exponential backoff delay
# ──────────────────────────────────────────────────────────


def connect(retries=CONNECT_RETRIES):
    """Open a modbus connection with our tunables; retry with backoff."""
    delay = 1.0
    for attempt in range(1, retries + 1):
        client = ModbusTcpClient(COUNTER_IP, port=PORT,
                                 timeout=TIMEOUT, retries=MODBUS_RETRIES)
        if client.connect():
            return client
        print(f"  [connect {attempt}/{retries}] could not reach "
              f"{COUNTER_IP}:{PORT}")
        try:
            client.close()
        except Exception:
            pass
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
    return None


def _reconnect(client):
    """Best-effort drop + re-open of the socket between retries."""
    try:
        client.close()
        time.sleep(0.5)
        if client.connect():
            print("    reconnected")
        else:
            print("    reconnect failed (will retry)")
    except Exception as e:
        print(f"    reconnect error: {e}")


def _retry(fn, *args, what='modbus call', retries=READ_RETRIES, client=None):
    """Call fn(*args) with retries, exponential backoff, and reconnect.

    Re-raises the last exception if every attempt fails.
    """
    delay = 1.0
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args)
        except Exception as e:
            last = e
            print(f"  [retry {attempt}/{retries}] {what} failed: {e}")
            if attempt < retries:
                if client is not None:
                    _reconnect(client)
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
    raise last


def safe_record_count(client):
    """Record count, or None if it genuinely could not be read.

    Distinct from 0: 0 means the counter is really empty, None means we could
    not talk to it — and None must NEVER lead to an erase.
    """
    try:
        return _retry(pp.get_record_count, client,
                      what='read record count', client=client)
    except Exception as e:
        print(f"  ERROR: could not read record count after {READ_RETRIES} "
              f"tries: {e}")
        return None


def count_csv_rows(path):
    """Number of DATA rows in the CSV (header excluded); 0 if missing/empty."""
    if not os.path.exists(path):
        return 0
    with open(path, newline='') as f:
        n = sum(1 for _ in csv.reader(f))
    return max(0, n - 1)


def _latch_and_read(client, i):
    """Latch record i, read it back, and confirm it IS record i.

    Verifying the returned record_number guards against a dropped latch-write
    on a flaky link silently handing us the wrong/previous record.
    """
    pp.latch_record(client, i)
    data = pp.read_latched_record(client)
    if data is None:
        raise RuntimeError(f'record {i}: empty / no response')
    rn = data.get('record_number')
    if str(rn) != str(i):
        raise RuntimeError(f'latch mismatch: requested {i}, got record_number {rn}')
    return data


def read_all_records(client, total):
    """Read records 1..total. Returns (records, failed_indices)."""
    records, failed = [], []
    for i in range(1, total + 1):
        try:
            data = _retry(_latch_and_read, client, i,
                          what=f'record {i}', client=client)
        except Exception as e:
            print(f"  [{i:4d}/{total}] FAILED after retries: {e}")
            failed.append(i)
            continue

        # Match the daemon's record schema exactly (sync_time is the final
        # column in measurement_archive.csv).
        data['sync_time'] = datetime.now().isoformat()
        records.append(data)
        print(f"  [{i:4d}/{total}] {data.get('date','?')} {data.get('time','?')}  "
              f"temp={data.get('temp_C','?')}C  RH={data.get('RH_pct','?')}%  "
              f"ch1_diff_m3={data.get('ch1_diff_m3','?')}")

    return records, failed


def verify_saved(path, records, rows_before):
    """Re-read the archive and confirm our records are now its last N rows."""
    n = len(records)
    rows_after = count_csv_rows(path)
    if rows_after - rows_before != n:
        print(f"  VERIFY FAIL: archive grew by {rows_after - rows_before} rows, "
              f"expected {n}")
        return False

    with open(path, newline='') as f:
        tail = list(csv.DictReader(f))[-n:]

    if len(tail) != n:
        print(f"  VERIFY FAIL: could only re-read {len(tail)} of {n} rows")
        return False

    for want, got in zip(records, tail):
        if str(want.get('record_number')) != str(got.get('record_number')):
            print(f"  VERIFY FAIL: record_number mismatch "
                  f"(expected {want.get('record_number')}, "
                  f"got {got.get('record_number')})")
            return False

    print(f"  VERIFY OK: {n} records confirmed in archive "
          f"({rows_before} → {rows_after} rows)")
    return True


def flush_and_erase(client):
    print(f"\n{'='*60}")
    print(f"Flush started: {datetime.now().isoformat()}")

    # ── 1) how many records are on the counter? ──────────────────────────────
    total = safe_record_count(client)
    if total is None:
        print("ABORT: counter not responding. Nothing read, NOTHING erased.")
        return False
    print(f"Records on counter: {total}")
    if total == 0:
        print("Counter is already empty — nothing to flush, nothing to erase.")
        return True

    # ── 2) read every record (all-or-nothing) ────────────────────────────────
    records, failed = read_all_records(client, total)
    if failed:
        print(f"\nABORT: {len(failed)}/{total} records failed to read: {failed}")
        print("Nothing saved, NOTHING erased. Fix the connection and re-run.")
        return False
    if len(records) != total:
        print(f"\nABORT: read {len(records)} records but expected {total}. "
              f"NOTHING erased.")
        return False

    # ── 3) append to the archive ─────────────────────────────────────────────
    rows_before = count_csv_rows(OUTPUT_CSV)
    if not pp.save_to_csv(records, OUTPUT_CSV):
        print("ABORT: save_to_csv reported failure. NOTHING erased.")
        return False

    # ── 4) VERIFY the data is really on disk before any erase ────────────────
    if not verify_saved(OUTPUT_CSV, records, rows_before):
        print("ABORT: could not verify the archive write. NOTHING erased.")
        return False

    print(f"\nAll {total} records saved AND verified in:\n  {OUTPUT_CSV}")

    # ── 5) erase — only now, only if explicitly enabled ──────────────────────
    if not ERASE_AFTER_SYNC:
        print("\nERASE_AFTER_SYNC=False → counter memory kept intact (no erase).")
        print("Confirm the data looks correct, set ERASE_AFTER_SYNC=True, re-run "
              "to wipe the counter.")
        return True

    print("\nData verified — erasing counter memory…")
    # pp.erase_counter writes the magic value, verifies 0 remaining, and resets
    # the daemon's counter_state.json so its next sync starts cleanly.
    if pp.erase_counter(client):
        print("Counter erased and confirmed empty (0 records remaining).")
        return True

    print("WARNING: erase could NOT be confirmed empty — check the counter "
          "manually before relying on it.")
    return False


def main():
    print("Particle Counter Flush + Erase Tool")
    print(f"  Target : {COUNTER_IP}:{PORT}")
    print(f"  Output : {OUTPUT_CSV}")
    print(f"  Erase  : {ERASE_AFTER_SYNC}")
    print(f"  Net    : timeout={TIMEOUT}s, modbus_retries={MODBUS_RETRIES}, "
          f"read_retries={READ_RETRIES}")
    print()

    client = connect()
    if client is None:
        print(f"ERROR: could not connect to the particle counter after "
              f"{CONNECT_RETRIES} tries. Aborting (nothing erased).")
        return 1

    print("Connected successfully")
    try:
        ok = flush_and_erase(client)
    except KeyboardInterrupt:
        print("\nInterrupted — counter NOT erased (safe).")
        ok = False
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}\nCounter NOT erased (safe).")
        ok = False
    finally:
        client.close()
        print("Connection closed")

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
