#!/usr/bin/env python3
"""
Verified auto-erase of the particle counter's onboard memory.

The counter logs a record every ~4 minutes, so its buffer fills up on its
own within weeks — the daemon has to clear it periodically. But erasing is
irreversible, so this module only erases after independently verifying that
every record the counter holds has actually landed in the permanent archive:

  1. the counter holds more than `cap` records (daemon's TRIM_CAP),
  2. the sync-state file confirms all `total` records were synced,
  3. the archive's most recent row really is record number `total`.

Called by particle_plus.mode_sync() after every successful sync. Safe to
call each cycle — it does nothing until all three checks pass, and the
erase itself (erase_counter) re-reads the counter to confirm it emptied.
"""

import csv
import os


def _archive_tail_record_number(archive_csv):
    """record_number of the archive's last data row (0 if unreadable)."""
    try:
        with open(archive_csv, 'rb') as f:
            header = f.readline().decode('utf-8', 'replace')
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(len(header.encode()), size - 8192))
            tail = f.read().decode('utf-8', 'replace').strip().splitlines()
        if not tail:
            return 0
        cols = next(csv.reader([header]))
        last = next(csv.reader([tail[-1]]))
        return int(float(last[cols.index('record_number')] or 0))
    except Exception:
        return 0


def verified_auto_erase(client, total, archive_csv, state_path, cap,
                        erase_fn, log, force=False):
    """
    Erase the counter only if every record is verifiably in the archive.
    Returns True when an erase happened and was confirmed.
    """
    if not (force or total > cap):
        return False

    from features.data_manager import get_last_synced

    last_synced = get_last_synced(state_path)
    if last_synced != total:
        log(f"Auto-erase skipped: sync state {last_synced} != "
            f"counter total {total}", 'WARN')
        return False

    tail_n = _archive_tail_record_number(archive_csv)
    if tail_n != total:
        log(f"Auto-erase skipped: archive last record {tail_n} != "
            f"counter total {total}", 'WARN')
        return False

    log(f"Counter at {total} records (cap {cap}) — all records verified "
        f"in archive, erasing counter memory")
    return erase_fn(client)
