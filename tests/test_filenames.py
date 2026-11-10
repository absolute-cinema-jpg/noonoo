"""File names typed for a download can't leave the download folder or confuse the filesystem."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _isolate  # noqa: F401  (before noonoo)

from noonoo import engine

PLAIN = {"restrict_filenames": False}
ASCII = {"restrict_filenames": True}


class CustomFilename(unittest.TestCase):
    def test_kept_as_typed(self):
        self.assertEqual(engine.custom_filename(PLAIN, "  Interview v1.2 take 3 "), "Interview v1.2 take 3")

    def test_typed_extension_dropped(self):
        self.assertEqual(engine.custom_filename(PLAIN, "Interview.MOV"), "Interview")
        self.assertEqual(engine.custom_filename(PLAIN, ".mp4"), "")

    def test_cant_leave_the_folder(self):
        for s in (PLAIN, ASCII):
            for bad in ("../../evil", "/etc/passwd", "a/../../b", "..", "."):
                name = engine.custom_filename(s, bad)
                self.assertNotIn("/", name)
                self.assertFalse(name.startswith("."))

    def test_control_characters_removed(self):
        self.assertEqual(engine.custom_filename(PLAIN, "a\x00b\nc"), "abc")

    def test_ascii_option(self):
        self.assertEqual(engine.custom_filename(ASCII, "Café: take 2"), "Cafe_take_2")

    def test_length_limit_keeps_whole_characters(self):
        name = engine.custom_filename(PLAIN, "é" * 300)
        self.assertLessEqual(len(name.encode()), 200)
        name.encode().decode()

    def test_nothing_typed(self):
        self.assertEqual(engine.custom_filename(PLAIN, None), "")
        self.assertEqual(engine.custom_filename(PLAIN, "   "), "")


if __name__ == "__main__":
    unittest.main()
