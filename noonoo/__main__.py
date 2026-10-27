"""Entry point.

    python3 -m noonoo            open NooNoo in your web browser
    python3 -m noonoo --serve    engine only, for the NooNoo.app window (prints its URL, exits when stdin closes)
"""
import argparse
import signal
import sys
import threading
import webbrowser

from . import engine as engine_mod
from . import settings as settings_mod
from . import tools
from .server import App, serve


def main():
    ap = argparse.ArgumentParser(prog="noonoo")
    ap.add_argument("--serve", action="store_true", help="run headless for the NooNoo.app window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-setup", action="store_true", help="don't auto-download missing tools on launch")
    opts = ap.parse_args()

    app = App()
    if tools.missing() and not opts.no_setup:  # first run: fetch yt-dlp/ffmpeg/deno in the background, UI shows progress
        app.install_tools()
    httpd, url = serve(app, opts.port)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    if opts.serve:
        engine_mod.NOTIFY_VIA_APP = True
        app.updater.start_background_checks()
        s = settings_mod.load()
        frame = "xp" if s["xp_frame"] else "native"
        # the app reads this line; when the app quits (or crashes) our stdin closes and we shut down cleanly
        print("NOONOO_READY %s&shell=app&frame=%s" % (url, frame), flush=True)
        threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    else:
        print("NooNoo running at", url)
        print("Press Ctrl+C to quit.")
        webbrowser.open(url)
    while not stop.wait(1):
        pass

    app.engine.shutdown()
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
