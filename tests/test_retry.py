"""Downloads the site turned away are tried again; real failures aren't."""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _isolate  # noqa: F401  (before noonoo)

from noonoo import engine

BOT = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. This helps protect our community."


class FakeEngine:
    _retry_refused = engine.Engine._retry_refused

    def __init__(self, results):
        self.lock = threading.Lock()
        self.results = list(results)
        self.calls = 0

    def _changed(self):
        pass

    def _download(self, j, s, with_login):
        self.calls += 1
        return self.results.pop(0)


def job():
    j = engine.Job(url="https://www.youtube.com/watch?v=abc")
    j.log, j._intent, j.proc = [], None, None
    return j


@mock.patch.object(engine, "RETRY_WAITS", (0, 0))
class RetryRefused(unittest.TestCase):
    def test_refused_then_works(self):
        e = FakeEngine([("ok", "")])
        self.assertEqual(e._retry_refused(job(), {}, False, "error", BOT), ("ok", ""))
        self.assertEqual(e.calls, 1)

    def test_gives_up_after_the_extra_tries(self):
        e = FakeEngine([("error", BOT), ("error", BOT)])
        self.assertEqual(e._retry_refused(job(), {}, False, "error", BOT)[0], "error")
        self.assertEqual(e.calls, 2)

    def test_403_on_video_data_is_retried(self):
        e = FakeEngine([("ok", "")])
        e._retry_refused(job(), {}, False, "error", "ERROR: unable to download video data: HTTP Error 403: Forbidden")
        self.assertEqual(e.calls, 1)

    def test_real_failures_are_not_retried(self):
        e = FakeEngine([])
        for err in ("ERROR: Unsupported URL: https://example.com", "ERROR: [youtube] abc: Private video"):
            self.assertEqual(e._retry_refused(job(), {}, False, "error", err), ("error", err))
        self.assertEqual(e.calls, 0)

    def test_success_untouched(self):
        e = FakeEngine([])
        self.assertEqual(e._retry_refused(job(), {}, False, "ok", ""), ("ok", ""))

    def test_pause_while_waiting_stops_it(self):
        j = job()
        j._intent = "pause"
        e = FakeEngine([])
        with mock.patch.object(engine, "RETRY_WAITS", (5,)):
            self.assertEqual(e._retry_refused(j, {}, False, "error", BOT), ("paused", ""))
        self.assertEqual(e.calls, 0)


FB = ("ERROR: [facebook] 1115732174431180: Cannot parse data; please report this issue on  "
      "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. "
      "Confirm you are on the latest version using  yt-dlp -U")
FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def login_settings(mode="fallback"):
    s = dict(engine.settings_mod.DEFAULTS)
    s.update(cookies_browser="brave", cookies_sites="youtube", cookies_mode=mode, notify_done=False)
    return s


@mock.patch.object(engine, "RETRY_WAITS", (0, 0))
class LoginBeforeRetrying(unittest.TestCase):
    """YouTube lists a video logged out, then refuses its picture with a 403 that never mentions signing in."""

    def run_job(self, s, results):
        e = engine.Engine()
        calls = []

        def fake(j, s, with_login):
            calls.append(with_login)
            return results.pop(0)
        with mock.patch.object(e, "_download", fake), mock.patch.object(e, "_pump"):
            j = job()
            e._run(j, s)
        return j, calls

    def test_403_logged_out_tries_the_login_first(self):
        j, calls = self.run_job(login_settings(), [("error", FORBIDDEN), ("ok", "")])
        self.assertEqual(calls, [False, True])
        self.assertEqual(j.used_login, "Brave")

    def test_login_also_refused_then_waits_and_retries_with_it(self):
        j, calls = self.run_job(login_settings(), [("error", FORBIDDEN), ("error", FORBIDDEN), ("ok", "")])
        self.assertEqual(calls, [False, True, True])

    def test_sites_not_ticked_never_get_the_login(self):
        s = login_settings()
        s["cookies_sites"] = "facebook"
        j, calls = self.run_job(s, [("error", FORBIDDEN), ("error", FORBIDDEN), ("error", FORBIDDEN)])
        self.assertEqual(calls, [False, False, False])
        self.assertEqual(j.status, "error")

    def test_time_outs_dont_lend_the_login(self):
        err = "ERROR: Read timed out."
        j, calls = self.run_job(login_settings(), [("error", err), ("error", err), ("error", err)])
        self.assertNotIn(True, calls)

    def test_facebook_login_wall_tries_the_login(self):
        s = login_settings()
        s["cookies_sites"] = "facebook"
        e = engine.Engine()
        calls = []

        def fake(j, s, with_login):
            calls.append(with_login)
            return ("ok", "") if with_login else ("error", FB)
        with mock.patch.object(e, "_download", fake), mock.patch.object(e, "_pump"):
            j = engine.Job(url="https://www.facebook.com/reel/1115732174431180")
            j.log, j._intent, j.proc = [], None, None
            e._run(j, s)
        self.assertEqual(calls, [False, True])

    def test_lookups_try_the_login_before_waiting(self):
        sr = engine.Searcher()
        seen = []

        def once(sid, args, s, with_login):
            seen.append(with_login)
            return ({"ok": 1}, "") if with_login else (None, "ERROR: HTTP Error 403: Forbidden")
        with mock.patch.object(sr, "_once", once):
            self.assertEqual(sr._run("x", [], login_settings(), "https://www.youtube.com/watch?v=abc", set()), {"ok": 1})
        self.assertEqual(seen, [False, True])


class Wording(unittest.TestCase):
    def test_bot_check_says_it_passes(self):
        s = dict(engine.settings_mod.DEFAULTS)
        msg = engine._friendly_error(BOT, s, "https://www.youtube.com/watch?v=abc")
        self.assertIn("usually clears", msg)
        self.assertNotIn("Update Download Engine", msg)

    def test_cannot_parse_drops_the_report_this_text(self):
        msg = engine._friendly_error(FB, dict(engine.settings_mod.DEFAULTS), "https://www.facebook.com/reel/1")
        self.assertNotIn("report this issue", msg)
        self.assertNotIn("yt-dlp -U", msg)
        self.assertIn("couldn't find the video", msg)

    def test_403_no_longer_blames_the_engine(self):
        msg = engine._friendly_error("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        self.assertNotIn("Update Download Engine", msg)


if __name__ == "__main__":
    unittest.main()
