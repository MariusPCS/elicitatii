"""Local server for the eLicitații viewer.

    python3 server.py            -> http://127.0.0.1:8765

Serves index.html + data/*.json and runs the scraper in a background thread
when the page's "Reîmprospătează" button is pressed (with a cooldown so the
button can't be used to hammer elicitatii.anaf.ro).
"""
import json
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scraper

ROOT = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8765
COOLDOWN = 10 * 60  # seconds between two refreshes

state = {"running": False, "etapa": None, "curent": None, "total": None,
         "pornit": None, "terminat": None, "eroare": None, "log": []}
lock = threading.Lock()


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with lock:
        state["log"] = (state["log"] + [line])[-40:]


def progress(**kw):
    with lock:
        state.update({"etapa": None, "curent": None, "total": None, "pagina": None})
        state.update(kw)


def run_refresh():
    try:
        scraper.refresh(log=log, progress=progress)
        err = None
    except Exception as e:
        traceback.print_exc()
        err = str(e)
        log("Error: " + err)
    with lock:
        state.update(running=False, terminat=time.time(), eroare=err, etapa=None)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            with lock:
                s = dict(state)
            last = s["terminat"] or 0
            s["urmatorulPosibil"] = max(0, round(last + COOLDOWN - time.time())) if not s["eroare"] else 0
            return self.send_json(200, s)
        if path in ("/", "/index.html") or path in ("/data/auctions.json", "/data/changelog.json"):
            if path.startswith("/data/") and not (ROOT / path.lstrip("/")).exists():
                return self.send_json(200, {} if "auctions" in path else [])
            return super().do_GET()
        self.send_error(404)

    def end_headers(self):
        if self.path.startswith("/data/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path != "/api/refresh":
            return self.send_error(404)
        with lock:
            if state["running"]:
                return self.send_json(409, {"mesaj": "A refresh is already running."})
            wait = (state["terminat"] or 0) + COOLDOWN - time.time()
            if wait > 0 and not state["eroare"]:
                return self.send_json(429, {"mesaj": f"The last refresh was recent. "
                                                     f"Try again in {int(wait // 60) + 1} min."})
        if not start_refresh():
            return self.send_json(409, {"mesaj": "A refresh is already running."})
        return self.send_json(202, {"mesaj": "Refresh started."})


def start_refresh():
    """Start a refresh unless one is running; returns False when skipped."""
    with lock:
        if state["running"]:
            return False
        state.update(running=True, pornit=time.time(), eroare=None, log=[])
    threading.Thread(target=run_refresh, daemon=True).start()
    return True


def auto_refresh(hours):
    while True:
        time.sleep(hours * 3600)
        log(f"Scheduled refresh (every {hours} h)")
        start_refresh()


if __name__ == "__main__":
    import argparse
    import webbrowser

    ap = argparse.ArgumentParser(description="Local viewer for ANAF eLicitații property auctions")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--auto", type=float, metavar="HOURS",
                    help="also refresh automatically every HOURS (minimum 1)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser on start")
    args = ap.parse_args()
    url = f"http://{HOST}:{args.port}"
    try:
        httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    except OSError:
        print(f"Port {args.port} is already in use – the viewer is probably already running. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        raise SystemExit(0)
    if args.auto:
        threading.Thread(target=auto_refresh, args=(max(1.0, args.auto),), daemon=True).start()
        print(f"Auto-refresh every {max(1.0, args.auto):g} h")
    print(f"eLicitații viewer running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
