"""Songs YouTube hides from logged-out searches are still found, and anything missing is reported."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _isolate  # noqa: F401  (before noonoo)

from noonoo import engine, spotify

TRACK = {"title": "Itchycoo Park", "artists": "Small Faces", "duration": 168, "id": "", "cover": "x", "date": "1967-08-04"}
HIT = {"id": "abc", "title": "Small Faces - Itchycoo Park", "channel": "Small Faces - Topic", "duration": 168,
       "url": "https://www.youtube.com/watch?v=abc"}


def settings(sites="youtube"):
    s = dict(engine.settings_mod.DEFAULTS)
    s.update(cookies_browser="brave", cookies_sites=sites, cookies_mode="fallback")
    return s


class FakeYouTube:
    """Answers like YouTube does today: 'Artist - Title' logged out gets the age check (no results)."""

    def __init__(self, reversed_works=True, login_works=True):
        self.calls = []
        self.reversed_works, self.login_works = reversed_works, login_works

    def __call__(self, sid, args, s, with_login):
        q = args[-1].split(":", 1)[1]
        self.calls.append((q, with_login))
        if with_login and self.login_works:
            return {"_type": "playlist", "entries": [HIT]}, ""
        if q == "Itchycoo Park Small Faces" and self.reversed_works:
            return {"_type": "playlist", "entries": [HIT]}, ""
        return {"_type": "playlist", "entries": []}, ""


def lookup(fake, s):
    sr = engine.Searcher()
    errors = []
    info = {"type": "playlist", "name": "Sixties", "cover": "", "tracks": [TRACK]}
    with mock.patch.object(spotify, "lookup", return_value=info), mock.patch.object(sr, "_once", fake):
        out = sr._spotify("x", "https://open.spotify.com/playlist/1", s, set(), errors)
    return out, errors


class AgeCheckedSearches(unittest.TestCase):
    def test_other_wording_found_logged_out(self):
        fake = FakeYouTube()
        out, errors = lookup(fake, settings())
        self.assertEqual(len(out), 1)
        self.assertEqual(errors, [])
        self.assertNotIn(True, [w for _, w in fake.calls])   # no login needed

    def test_login_when_no_wording_works(self):
        fake = FakeYouTube(reversed_works=False)
        out, errors = lookup(fake, settings())
        self.assertEqual(len(out), 1)
        self.assertEqual(fake.calls[-1], ("Small Faces - Itchycoo Park", True))

    def test_login_not_allowed_reports_it_missing(self):
        fake = FakeYouTube(reversed_works=False)
        out, errors = lookup(fake, settings(sites="facebook"))
        self.assertEqual(out, [])
        self.assertEqual(errors, ["Small Faces - Itchycoo Park: not found on YouTube"])
        self.assertNotIn(True, [w for _, w in fake.calls])

    def test_search_failure_says_why(self):
        sr = engine.Searcher()
        errors = []
        info = {"type": "playlist", "name": "Sixties", "cover": "", "tracks": [TRACK]}
        with mock.patch.object(spotify, "lookup", return_value=info), \
             mock.patch.object(engine, "SEARCH_RETRY_WAITS", ()), \
             mock.patch.object(sr, "_once", return_value=(None, "ERROR: Unable to download API page")):
            sr._spotify("x", "https://open.spotify.com/playlist/1", settings(sites=""), set(), errors)
        self.assertIn("couldn't search YouTube", errors[0])

    def test_keyword_search_tries_the_login_when_empty(self):
        sr = engine.Searcher()
        fake = FakeYouTube(reversed_works=False)
        with mock.patch.object(sr, "_once", fake), mock.patch.object(engine.settings_mod, "load", return_value=settings()):
            r = sr.search("x", "Small Faces - Itchycoo Park")
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["login"], ["youtube.com"])

    def test_empty_search_without_login_is_just_empty(self):
        sr = engine.Searcher()
        fake = FakeYouTube(reversed_works=False)
        with mock.patch.object(sr, "_once", fake), \
             mock.patch.object(engine.settings_mod, "load", return_value=settings(sites="")):
            self.assertEqual(sr.search("x", "Small Faces - Itchycoo Park")["items"], [])


class FailureNotification(unittest.TestCase):
    def test_failed_download_notifies(self):
        e = engine.Engine()
        j = engine.Job(url="https://www.youtube.com/watch?v=abc", title="Itchycoo Park")
        j.log, j._intent, j.proc = [], None, None
        s = settings(sites="")
        with mock.patch.object(e, "_download", return_value=("error", "ERROR: Private video")), \
             mock.patch.object(e, "_pump"), mock.patch.object(engine, "notify") as n:
            e._run(j, s)
        n.assert_called_once()
        self.assertEqual(n.call_args[0][0], "Download failed")
        self.assertIn("Itchycoo Park", n.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
