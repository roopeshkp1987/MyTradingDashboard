"""
NSE Market Dashboard — Local Server
=====================================
Serves the dashboard frontend and provides API proxy endpoints.

Usage:    python server.py
Then open: http://localhost:8080

Endpoints:
  GET  /                   → Dashboard (index.html)
  GET  /api/status         → Server status + last refresh time
  POST /api/refresh        → Trigger fetch_data.py in background
  GET  /api/refresh/status → Check if refresh is running + recent log lines
  GET  /api/chart          → Proxy Yahoo Finance chart API (CORS-free, curl_cffi)
"""
import http.server
import urllib.parse
import subprocess
import threading
import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Force UTF-8 output on Windows to avoid UnicodeEncodeError in terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# curl_cffi: Chrome-impersonation session for Yahoo Finance (bypasses rate limits)
try:
    from curl_cffi import requests as cffi_requests
    _chart_session = cffi_requests.Session(impersonate="chrome")
    _chart_backend = "curl_cffi ✓"
except ImportError:
    import urllib.request
    _chart_session = None
    _chart_backend = "urllib (install curl_cffi for better reliability)"

PORT    = 8080
APP_DIR = Path(__file__).parent.resolve()
DATA_FILE    = APP_DIR / "data" / "stocks.json"
FETCH_SCRIPT = APP_DIR / "scripts" / "fetch_data.py"

# ─── Refresh state (shared across requests) ───────────────────────────────────
_refresh_lock    = threading.Lock()
_refresh_running = False
_refresh_log     = []          # Recent log lines
_refresh_start   = None
_refresh_end     = None
_refresh_ok      = None        # True/False/None


class DashboardHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def log_message(self, fmt, *args):
        # Only log API calls
        if self.path.startswith("/api/"):
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {self.command} {self.path}  {args[1]}", flush=True)

    # ── Routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            self._handle_status()
        elif path == "/api/refresh/status":
            self._handle_refresh_status()
        elif path == "/api/chart":
            self._handle_chart()
        else:
            if path in ("/", ""):
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/refresh":
            self._handle_refresh_trigger()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._add_cors()
        self.end_headers()

    # ── Helper: send JSON ─────────────────────────────────────────────────────
    def _add_cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._add_cors()
        self.end_headers()
        self.wfile.write(body)

    # ── /api/status ───────────────────────────────────────────────────────────
    def _handle_status(self):
        last_updated = None
        total_stocks = 0
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                last_updated = d.get("last_updated")
                total_stocks = d.get("total", 0)
            except Exception:
                pass

        self._send_json({
            "status":          "ok",
            "refresh_running": _refresh_running,
            "last_updated":    last_updated,
            "total_stocks":    total_stocks,
            "data_exists":     DATA_FILE.exists(),
        })

    # ── POST /api/refresh ─────────────────────────────────────────────────────
    def _handle_refresh_trigger(self):
        global _refresh_running, _refresh_log, _refresh_start, _refresh_end, _refresh_ok

        with _refresh_lock:
            if _refresh_running:
                self._send_json({
                    "status":  "already_running",
                    "message": "A refresh is already in progress. Check /api/refresh/status"
                })
                return
            _refresh_running = True
            _refresh_log     = ["[INFO] Starting fetch_data.py …"]
            _refresh_start   = datetime.now().isoformat()
            _refresh_end     = None
            _refresh_ok      = None

        def _run():
            global _refresh_running, _refresh_log, _refresh_end, _refresh_ok
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(FETCH_SCRIPT)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(APP_DIR),
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    print(f"  {line}", flush=True)
                    _refresh_log.append(line)
                    if len(_refresh_log) > 500:   # cap log size
                        _refresh_log = _refresh_log[-500:]
                proc.wait()
                _refresh_ok  = (proc.returncode == 0)
                _refresh_end = datetime.now().isoformat()
                status_msg   = "completed" if _refresh_ok else f"failed (exit {proc.returncode})"
                _refresh_log.append(f"[INFO] Refresh {status_msg}")
                print(f"[REFRESH] {status_msg}", flush=True)
            except Exception as exc:
                _refresh_ok  = False
                _refresh_end = datetime.now().isoformat()
                _refresh_log.append(f"[ERROR] {exc}")
                print(f"[REFRESH ERROR] {exc}", flush=True)
            finally:
                _refresh_running = False

        threading.Thread(target=_run, daemon=True).start()
        self._send_json({"status": "started", "message": "Refresh started — poll /api/refresh/status"})

    # ── GET /api/refresh/status ───────────────────────────────────────────────
    def _handle_refresh_status(self):
        self._send_json({
            "running":    _refresh_running,
            "ok":         _refresh_ok,
            "started_at": _refresh_start,
            "ended_at":   _refresh_end,
            "log":        _refresh_log[-50:],   # Last 50 lines
        })

    # ── GET /api/chart ────────────────────────────────────────────────────────────────
    def _handle_chart(self):
        """Proxy Yahoo Finance chart API — resolves browser CORS restrictions."""
        qs     = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        symbol = qs.get("symbol",   ["RELIANCE.NS"])[0]
        range_ = qs.get("range",    ["1y"])[0]
        intvl  = qs.get("interval", ["1d"])[0]

        yf_headers = {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept":          "application/json, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://finance.yahoo.com/",
        }

        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = (
                f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol)}"
                f"?range={range_}&interval={intvl}&includeAdjustedClose=true"
            )
            try:
                if _chart_session is not None:
                    # curl_cffi path — impersonates browser, bypasses Cloudflare
                    resp = _chart_session.get(url, headers=yf_headers, timeout=20)
                    body = resp.content
                    if resp.status_code != 200:
                        continue
                else:
                    # Fallback: standard urllib
                    import urllib.request as _ur
                    req = _ur.Request(url, headers=yf_headers)
                    with _ur.urlopen(req, timeout=20) as r:
                        body = r.read()

                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._add_cors()
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                print(f"[CHART] {host} failed: {e}", flush=True)
                continue

        self._send_json({"error": "Failed to fetch chart data from Yahoo Finance"}, status=502)


# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    os.chdir(APP_DIR)
    banner = f"""
╔══════════════════════════════════════════════╗
║   NSE Market Dashboard — Local Server        ║
║                                              ║
║   Dashboard:  http://localhost:{PORT}           ║
║   Status API: http://localhost:{PORT}/api/status║
║                                              ║
║   Press Ctrl+C to stop                       ║
╚══════════════════════════════════════════════╝"""
    print(banner, flush=True)
    print(f"  Chart backend : {_chart_backend}", flush=True)
    print(f"  Data file     : {'EXISTS ✓' if DATA_FILE.exists() else 'MISSING — run fetch_data.py first'}", flush=True)

    if not DATA_FILE.exists():
        print(f"\n[WARN] stocks.json not found. Click 'Refresh Market Data' in the dashboard", flush=True)
        print(f"       or run:  python scripts/fetch_data.py\n", flush=True)

    httpd = http.server.HTTPServer(("", PORT), DashboardHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] Stopped.", flush=True)


if __name__ == "__main__":
    main()
