"""Import this before noonoo in every test file, so tests never touch the real settings or downloads.

NooNoo works out its data folder when noonoo.settings is first imported, and test files run in
alphabetical order, so it only takes one file importing noonoo first to aim every test at real data.
"""
import os
import sys
import tempfile

if "noonoo.settings" in sys.modules:
    raise RuntimeError("noonoo was imported before tests/_isolate.py, so tests would use your real settings. "
                       "Add `import _isolate` above the noonoo import in that test file.")

os.environ["HOME"] = tempfile.mkdtemp(prefix="noonoo-test-")
