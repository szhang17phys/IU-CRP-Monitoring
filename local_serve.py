#!/usr/bin/env python3
"""
local_serve.py — noether-only FULL-HISTORY dashboard server.

Unlike the public GitHub Pages dashboard (30-day window), this serves the
complete local measurement archive — including data that must never leave
noether (and, later, coldbox / slow-control datasets too large to publish).

SECURITY MODEL
    The server binds STRICTLY to 127.0.0.1 — it is never reachable from the
    network, even from other machines on the lab LAN. To view it from your
    own computer, forward the port over SSH:

        ssh -L 8800:localhost:8800 <user>@noether
        # then open  http://localhost:8800  in your local browser

    Do NOT change the bind address to 0.0.0.0 / '' — that would expose the
    archive to anyone who can reach the machine.

BEHAVIOR
    * Regenerates index_local.html every 60 s from the full archive
      (data/measurement_archive.csv), so the page's 60 s auto-reload always
      shows fresh data while the daemon keeps sampling.
    * index_local.html is gitignored — it never reaches GitHub.
    * Reuses particle_plus.generate_dashboard_html() with days=None /
      env_days=None / local=True (extended time ranges incl. "All data",
      LOCAL header badge, binning available on every window).
    * The running daemon (particle_plus.py --all) is untouched; this script
      only READS the data files.

Usage (on noether, e.g. inside tmux):
    python3 local_serve.py              # default port 8800
    python3 local_serve.py --port 9000
"""

import argparse
import os
import sys
import threading
import time
import http.server

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BIND_ADDR = '127.0.0.1'          # loopback ONLY — see security note above
LOCAL_HTML_NAME = 'index_local.html'
LOCAL_HTML = os.path.join(BASE_DIR, LOCAL_HTML_NAME)
REGEN_INTERVAL_S = 10            # matches the page's 60 s auto-reload

sys.path.insert(0, BASE_DIR)
import particle_plus as pp


def csv_source():
    """Full archive if present (noether), else the 30-day live file."""
    return pp.ARCHIVE_CSV if os.path.exists(pp.ARCHIVE_CSV) else pp.LIVE_CSV


def rebuild():
    """Regenerate index_local.html over the FULL history."""
    src = csv_source()
    print(f'[local] Rebuilding {LOCAL_HTML_NAME} from {os.path.basename(src)} (full history) …')
    return pp.generate_dashboard_html(src, LOCAL_HTML,
                                      days=None, env_days=None, local=True)


def _regen_loop():
    """Background thread: refresh the page every REGEN_INTERVAL_S seconds."""
    while True:
        time.sleep(REGEN_INTERVAL_S)
        try:
            rebuild()
        except Exception as e:                          # keep serving old page
            print(f'[local] WARNING: rebuild failed: {e}')


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the repo dir but maps the root URL to index_local.html."""

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.path = '/' + LOCAL_HTML_NAME
        return super().do_GET()

    def do_HEAD(self):
        if self.path in ('/', '/index.html'):
            self.path = '/' + LOCAL_HTML_NAME
        return super().do_HEAD()

    def log_message(self, fmt, *args):
        pass                                            # silence request logs


def serve(port):
    os.chdir(BASE_DIR)
    httpd = http.server.ThreadingHTTPServer((BIND_ADDR, port), LocalHandler)
    print(f'[local] Full-history dashboard → http://localhost:{port}')
    print(f'[local] Bound to {BIND_ADDR} only. From another machine:')
    print(f'[local]     ssh -L {port}:localhost:{port} <user>@noether')
    print(f'[local] Regenerating every {REGEN_INTERVAL_S} s. Ctrl-C to stop.')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[local] Stopped.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='noether-only full-history dashboard')
    parser.add_argument('--port', type=int, default=8800,
                        help='Port on 127.0.0.1 (default: 8800)')
    args = parser.parse_args()

    if not rebuild():
        print('[local] WARNING: initial rebuild reported failure — serving anyway.')

    threading.Thread(target=_regen_loop, daemon=True).start()
    serve(args.port)
