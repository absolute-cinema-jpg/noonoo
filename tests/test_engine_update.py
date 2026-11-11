"""Automatic yt-dlp updates: old copies are only cleared once nothing can be using them."""
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _isolate  # noqa: F401  (before noonoo)

from noonoo import tools


class PruneOldBuilds(unittest.TestCase):
    def setUp(self):
        tools.BIN_DIR.mkdir(parents=True, exist_ok=True)
        for d in ("yt-dlp-dist", "yt-dlp-dist-1", "yt-dlp-dist-2"):
            (tools.BIN_DIR / d).mkdir(exist_ok=True)
        tools.path("yt-dlp").write_text('#!/bin/sh\nexec "$(dirname "$0")/yt-dlp-dist-2/yt-dlp_macos" "$@"\n')

    def tearDown(self):
        shutil.rmtree(tools.BIN_DIR, ignore_errors=True)

    def test_keeps_only_the_current_build(self):
        tools.prune_old_builds()
        self.assertEqual(sorted(d.name for d in tools.BIN_DIR.glob("yt-dlp-dist*")), ["yt-dlp-dist-2"])

    def test_unreadable_wrapper_removes_nothing(self):
        tools.path("yt-dlp").write_text("something else")
        tools.prune_old_builds()
        self.assertEqual(len(list(tools.BIN_DIR.glob("yt-dlp-dist*"))), 3)


class UpdateYtdlp(unittest.TestCase):
    def test_up_to_date_downloads_nothing(self):
        with mock.patch.object(tools, "_ytdlp_release", return_value=tools.YTDLP_RELEASE % "2026.10.26"), \
             mock.patch.object(tools, "version", return_value="2026.10.26"), \
             mock.patch.object(tools, "_install") as install:
            self.assertIsNone(tools.update_ytdlp())
        install.assert_not_called()

    def test_newer_release_installed(self):
        with mock.patch.object(tools, "_ytdlp_release", return_value=tools.YTDLP_RELEASE % "2026.11.30"), \
             mock.patch.object(tools, "version", return_value="2026.10.26"), \
             mock.patch.object(tools, "_install") as install:
            self.assertEqual(tools.update_ytdlp(), "2026.11.30")
        install.assert_called_once()


if __name__ == "__main__":
    unittest.main()
