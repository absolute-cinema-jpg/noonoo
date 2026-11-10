"""Local HTTP API + static UI. Bound to 127.0.0.1 and guarded by a per-launch token."""
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import engine as engine_mod
from . import settings as settings_mod
from . import tools
from . import updater as updater_mod

WEB_DIR = Path(__file__).parent / "web"
TOKEN = secrets.token_urlsafe(24)
# Remote images are search thumbnails. Scripts must be ours, so injected markup can't run code.
CSP = ("default-src 'none'; script-src 'self' 'nonce-%s'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https: http:; connect-src 'self'; font-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class App:
    def __init__(self):
        self.engine = engine_mod.Engine()
        self.updater = updater_mod.Updater()
        self.searcher = engine_mod.Searcher()
        self.tool_log = []
        self.tools_busy = False
        self.window = None  # set by the pywebview launcher

    def install_tools(self, names=None, update=False):
        if self.tools_busy:
            return
        self.tools_busy = True

        def log(msg):
            self.tool_log.append(msg)
            del self.tool_log[:-50]

        def work():
            try:
                if update:
                    tools.update(log)
                else:
                    tools.ensure(log, names)
                log("Done.")
            finally:
                self.tools_busy = False

        threading.Thread(target=work, daemon=True).start()


def clean_folder(raw):
    """A folder path as typed or pasted -> {"path", "exists"}, or {"error"} saying what's wrong with it."""
    p = (raw or "").strip()
    if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'":     # 'quoted' or "quoted"
        p = p[1:-1].strip()
    if p.lower().startswith("file://"):                    # a dragged or copied file URL
        p = unquote(urlparse(p).path)
    if re.search(r"[\x00-\x1f]", p):
        return {"error": "That doesn't look like a folder path."}
    if not os.path.exists(os.path.expanduser(p)):
        p = re.sub(r"\\(.)", r"\1", p)                      # copied from Terminal: My\ Folder
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        return {"error": "Enter the full path to the folder, such as ~/Movies or /Volumes/Drive/Media."}
    p = os.path.normpath(p)
    if os.path.exists(p) and not os.path.isdir(p):
        return {"error": "That's a file, not a folder: %s" % p}
    parent = p
    while not os.path.exists(parent):                      # new folders are created when a download lands
        parent = os.path.dirname(parent)
    if not os.access(parent, os.W_OK):
        return {"error": "NooNoo can't save there, because it can't write to %s. If it's on a drive, is the drive connected?" % parent}
    return {"path": p, "exists": os.path.isdir(p)}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ---- helpers -------------------------------------------------------
        def _send(self, code, body=b"", ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authed(self, q):
            return secrets.compare_digest(self.headers.get("X-Token") or (q.get("t") or [""])[0], TOKEN)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---- GET -----------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if u.path == "/":
                if not self._authed(q):
                    return self._send(403, "Forbidden", "text/plain")
                nonce = secrets.token_urlsafe(16)
                html = (WEB_DIR / "index.html").read_text().replace("__TOKEN__", TOKEN).replace("__NONCE__", nonce)
                return self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": CSP % nonce})
            if u.path.startswith("/static/"):
                f = (WEB_DIR / u.path[len("/static/"):]).resolve()
                if WEB_DIR.resolve() not in f.parents or not f.is_file():
                    return self._send(404, {"error": "not found"})
                return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
            if not self._authed(q):
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/state":
                snap = app.engine.snapshot()
                snap["tools_busy"] = app.tools_busy
                snap["update"] = app.updater.snapshot()
                return self._send(200, snap)
            if u.path == "/api/job":
                return self._send(200, app.engine.job((q.get("id") or [""])[0], with_log=True) or {})
            if u.path == "/api/settings":
                return self._send(200, {"settings": settings_mod.load(), "defaults": settings_mod.DEFAULTS,
                                        "video_formats": {k: v["label"] for k, v in engine_mod.VIDEO_FORMATS.items()},
                                        "edit_formats": [k for k, v in engine_mod.VIDEO_FORMATS.items() if v.get("edit")],
                                        "audio_formats": engine_mod.AUDIO_FORMATS,
                                        "cookie_sites": {k: v[0] for k, v in settings_mod.COOKIE_SITES.items()},
                                        "home": str(Path.home())})
            if u.path == "/api/tools":
                return self._send(200, {"status": tools.status(), "missing": tools.missing(),
                                        "busy": app.tools_busy, "log": app.tool_log[-20:],
                                        "bin": str(tools.BIN_DIR)})
            return self._send(404, {"error": "not found"})

        do_HEAD = do_GET

        # ---- POST ----------------------------------------------------------
        def do_POST(self):
            u = urlparse(self.path)
            if not self._host_ok() or not self._authed({}):
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                return self._send(200, self._route(u.path, body))
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def _route(self, path, b):
            s = settings_mod.load()
            if path == "/api/search":
                if tools.missing():
                    raise RuntimeError("Download engine not installed yet - see Tools > Engine.")
                return app.searcher.search(b.get("id") or "x", b.get("query", ""), b.get("source", "youtube"))
            if path == "/api/search/details":
                return {"details": app.searcher.details(b.get("id") or "x", [u for u in b.get("urls", []) if u][:10],
                                                        b.get("kind", "video"))}
            if path == "/api/update/check":
                return app.updater.check()
            if path == "/api/update/download":
                return app.updater.download()
            if path == "/api/update/skip":
                app.updater.skip(b.get("version", ""))
                return app.updater.snapshot()
            if path == "/api/update/install":
                return app.updater.install(app.engine)
            if path == "/api/search/cancel":
                app.searcher.cancel(b.get("id"))
                return {"ok": True}
            if path == "/api/download":
                return {"ids": app.engine.add(b.get("items", []), b.get("kind", "video"), b.get("quality", ""),
                                              b.get("vformat", ""), b.get("aformat", ""), b.get("folder", ""),
                                              b.get("fps", ""))}
            if path == "/api/job/action":
                ids = b.get("ids") or [b.get("id")]
                for jid in ids:
                    app.engine.action(jid, b.get("action"))
                return {"ok": True}
            if path == "/api/jobs/bulk":
                app.engine.bulk(b.get("action"))
                return {"ok": True}
            if path == "/api/settings":
                return {"settings": settings_mod.save(dict(s, **b.get("settings", {})))}
            if path == "/api/settings/reset":
                return {"settings": settings_mod.save({})}
            if path == "/api/tools/install":
                app.install_tools(update=bool(b.get("update")))
                return {"ok": True}
            if path == "/api/open":
                return self._open(b, s)
            if path == "/api/check-folder":
                return clean_folder(b.get("path"))
            if path == "/api/choose-folder":
                return {"path": choose_folder(b.get("start") or s["output_dir"])}
            if path == "/api/window":
                return window_action(app, b.get("action"), b)
            raise RuntimeError("unknown endpoint")

        def _open(self, b, s):
            what, target = b.get("what"), b.get("path") or ""
            if what == "url":
                if not re.match(r"^https?://", target):
                    raise RuntimeError("not a web link")
                subprocess.Popen(["open", target])
            elif what == "folder":
                d = os.path.expanduser(target or s["output_dir"])
                known = {os.path.realpath(x.folder) for x in app.engine.jobs.values() if x.folder}
                known.add(os.path.realpath(os.path.expanduser(s["output_dir"])))
                if target and (d.endswith((".app", ".app/")) or os.path.realpath(d) not in known):
                    raise RuntimeError("Not one of your download folders")
                os.makedirs(d, exist_ok=True)
                subprocess.Popen(["open", d])
            elif what in ("file", "reveal"):
                downloaded = {os.path.realpath(f) for f in app.engine.known_files()}
                if os.path.realpath(target) not in downloaded or not os.path.exists(target):
                    raise RuntimeError("File not found (moved or deleted?)")
                subprocess.Popen(["open", target] if what == "file" else ["open", "-R", target])
            return {"ok": True}

    return Handler


def choose_folder(start):
    start = os.path.expanduser(start)
    while start and not os.path.isdir(start):
        start = os.path.dirname(start)
    script = ('POSIX path of (choose folder with prompt "Save downloads to:" default location (POSIX file %s))'
              % json.dumps(start or str(Path.home())))
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return out.stdout.strip().rstrip("/") or None


def window_action(app, action, args=None):
    args = args or {}
    w = app.window
    if not w:
        return {"ok": False}
    if action == "minimize":
        w.minimize()
    elif action == "maximize":
        maxed = getattr(app, "_maxed", False)
        try:
            (w.restore if maxed else w.maximize)()
        except AttributeError:
            w.toggle_fullscreen()
        app._maxed = not maxed
    elif action == "resize":
        w.resize(int(args.get("w", 1100)), int(args.get("h", 720)))
    elif action == "close":
        threading.Thread(target=lambda: (time.sleep(0.05), w.destroy()), daemon=True).start()
    return {"ok": True}


def serve(app, port=0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/?t=%s" % (httpd.server_address[1], TOKEN)
