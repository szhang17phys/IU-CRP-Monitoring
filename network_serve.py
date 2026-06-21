#!/usr/bin/env python3
"""
network_serve.py — Network-accessible dashboard server (Yale network only)

IMPORTANT: This server is accessible to ANYONE on the Yale network!
Unlike local_serve.py (localhost only), this binds to the network interface.

SECURITY MODEL:
    Binds to 0.0.0.0 (all interfaces) on specified port.
    Anyone who can reach this machine on the network can view the dashboard.

    For lab monitoring, this is usually fine:
    - Read-only access (no control of equipment)
    - Already behind Yale firewall
    - Useful for lab members to check status

    To restrict access, use firewall rules or add authentication.

USAGE:
    # Start server (runs 24/7 in background)
    tmux new -s dashboard
    python3 network_serve.py --port 8800
    # Ctrl+B, D to detach

    # Lab members access via browser:
    http://noether.physics.yale.edu:8800
    or
    http://10.x.x.x:8800  (noether's IP)

    # To stop:
    tmux attach -t dashboard
    Ctrl+C
"""

import argparse
import os
import sys
import time
import http.server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_HTML_NAME = 'index_local.html'
LOCAL_HTML = os.path.join(BASE_DIR, LOCAL_HTML_NAME)
REGEN_INTERVAL_S = 60  # Rebuild dashboard every 60s

sys.path.insert(0, BASE_DIR)
import particle_plus as pp


def csv_source():
    """Full archive if present, else live file."""
    return pp.ARCHIVE_CSV if os.path.exists(pp.ARCHIVE_CSV) else pp.LIVE_CSV


def rebuild():
    """Regenerate index_local.html over the FULL history."""
    src = csv_source()
    print(f'[network] Rebuilding {LOCAL_HTML_NAME} from {os.path.basename(src)} (full history)...')
    return pp.generate_dashboard_html(src, LOCAL_HTML, days=None, env_days=None, local=True)


def _regen_loop():
    """Background thread: refresh dashboard every 60s."""
    import threading
    def loop():
        while True:
            time.sleep(REGEN_INTERVAL_S)
            try:
                rebuild()
            except Exception as e:
                print(f'[network] WARNING: rebuild failed: {e}')

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


class NetworkHandler(http.server.SimpleHTTPRequestHandler):
    """Serves repo directory, maps root to index_local.html."""

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.path = '/' + LOCAL_HTML_NAME
        return super().do_GET()

    def do_HEAD(self):
        if self.path in ('/', '/index.html'):
            self.path = '/' + LOCAL_HTML_NAME
        return super().do_HEAD()

    def log_message(self, fmt, *args):
        # Log requests to stdout
        print(f'[network] {self.address_string()} - {fmt % args}')


def serve(port, bind_addr='0.0.0.0'):
    """Start network-accessible HTTP server."""
    os.chdir(BASE_DIR)

    try:
        httpd = http.server.ThreadingHTTPServer((bind_addr, port), NetworkHandler)
    except OSError as e:
        if 'Address already in use' in str(e):
            print(f'[network] ERROR: Port {port} is already in use!')
            print(f'[network] Try a different port or stop the existing server.')
            return False
        raise

    # Get actual hostname/IP for display
    import socket
    hostname = socket.gethostname()

    print('╔═══════════════════════════════════════════════════════════════════╗')
    print('║         Network-Accessible Dashboard Server (Yale Network)       ║')
    print('╚═══════════════════════════════════════════════════════════════════╝')
    print()
    print(f'[network] Dashboard running on port {port}')
    print(f'[network] Bound to: {bind_addr}:{port}')
    print(f'[network] Auto-refresh: every {REGEN_INTERVAL_S}s')
    print()
    print('Access from Yale network:')
    print(f'  → http://{hostname}:{port}')
    print(f'  → http://{hostname}.physics.yale.edu:{port}')
    print()
    print('⚠️  SECURITY NOTE:')
    print('   Anyone on Yale network can access this dashboard.')
    print('   Dashboard is READ-ONLY (safe for monitoring).')
    print()
    print('To stop: Ctrl+C')
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[network] Stopped.')
        return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Network-accessible dashboard (Yale network only)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8800,
        help='Port number (default: 8800)'
    )
    parser.add_argument(
        '--bind',
        type=str,
        default='0.0.0.0',
        help='Bind address (default: 0.0.0.0 = all interfaces)'
    )
    args = parser.parse_args()

    # Initial rebuild
    if not rebuild():
        print('[network] WARNING: initial rebuild reported failure')

    # Start background refresh
    _regen_loop()

    # Start server
    serve(args.port, args.bind)
