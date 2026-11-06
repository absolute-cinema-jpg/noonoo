"""Security checks. No network, and no real keys or settings are touched.

    python3 tests/test_security.py
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOME"] = tempfile.mkdtemp(prefix="noonoo-test-")  # before NooNoo works out its data folder

from noonoo import server, settings, tools, updater, verify  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
OPENSSL = "/usr/bin/openssl"


def make_key(folder, name="key"):
    private, public = Path(folder) / (name + ".pem"), Path(folder) / (name + ".pub")
    params = subprocess.run([OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout"],
                            capture_output=True, check=True).stdout
    private.write_bytes(params)
    subprocess.run([OPENSSL, "ec", "-in", str(private), "-pubout", "-out", str(public)], capture_output=True, check=True)
    return private, public


def sign(private, target):
    out = str(target) + ".sig"
    subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(private), "-out", out, str(target)], check=True)
    return out


class UpdateSignature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.private, self.public = make_key(self.dir)
        self.zip = self.dir / "NooNoo.zip"
        self.zip.write_bytes(b"pretend app" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_signature(self):
        verify.verify_update(self.zip, sign(self.private, self.zip), self.public)

    def test_tampered_archive(self):
        sig = sign(self.private, self.zip)
        self.zip.write_bytes(b"malicious app" * 100)
        with self.assertRaises(verify.VerificationError):
            verify.verify_update(self.zip, sig, self.public)

    def test_signed_by_someone_else(self):
        other, _ = make_key(self.dir, "attacker")
        with self.assertRaises(verify.VerificationError):
            verify.verify_update(self.zip, sign(other, self.zip), self.public)

    def test_garbage_signature(self):
        bad = self.dir / "bad.sig"
        bad.write_bytes(b"not a signature")
        with self.assertRaises(verify.VerificationError):
            verify.verify_update(self.zip, bad, self.public)

    def test_no_key_shipped_fails_closed(self):
        with self.assertRaises(verify.VerificationError):
            verify.verify_update(self.zip, sign(self.private, self.zip), self.dir / "missing.pem")


class UpdaterFlow(unittest.TestCase):
    """The real download path, against a fake feed: what a hijacked GitHub release looks like."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.private, self.public = make_key(self.dir)
        self.app = self.dir / "src" / "NooNoo.app"
        contents = self.app / "Contents"
        contents.mkdir(parents=True)
        import plistlib
        (contents / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": "9.9.9", "CFBundleIdentifier": updater.BUNDLE_ID}))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(self.app)], capture_output=True, check=True)
        self.archive = self.dir / "NooNoo-9.9.9-macOS-Test.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(self.app), str(self.archive)], check=True)
        self.stage = mock.patch.object(updater, "STAGE_DIR", self.dir / "stage")
        self.key = mock.patch.object(verify, "UPDATE_KEY", self.public)
        self.stage.start()
        self.key.start()
        self.signed_key_default = verify.verify_update.__defaults__
        verify.verify_update.__defaults__ = (self.public,)

    def tearDown(self):
        verify.verify_update.__defaults__ = self.signed_key_default
        self.stage.stop()
        self.key.stop()
        self.tmp.cleanup()

    def run_download(self, sig_path):
        u = updater.Updater()
        u._asset = {"name": self.archive.name, "browser_download_url": str(self.archive), "size": self.archive.stat().st_size}
        u._signature = {"browser_download_url": str(sig_path)} if sig_path else None
        u.state.update(status="available", latest="9.9.9")

        def fake_open(url, proxy, timeout=20):
            return open(url, "rb")

        with mock.patch.object(updater, "_open", fake_open):
            u._download()
        return u.snapshot()

    def test_signed_update_installs(self):
        state = self.run_download(sign(self.private, self.archive))
        self.assertEqual(state["status"], "ready", state["error"])

    def test_unsigned_update_refused(self):
        state = self.run_download(None)
        self.assertEqual(state["status"], "available")
        self.assertIn("isn't signed", state["error"])
        self.assertFalse((updater.STAGE_DIR / "new").exists())

    def test_wrongly_signed_update_refused_before_unpacking(self):
        other, _ = make_key(self.dir, "attacker")
        state = self.run_download(sign(other, self.archive))
        self.assertEqual(state["status"], "available")
        self.assertIn("signature", state["error"])
        self.assertFalse((updater.STAGE_DIR / "new").exists())


class YtDlpSignature(unittest.TestCase):
    def setUp(self):
        self.sums = (FIXTURES / "yt-dlp-SHA2-256SUMS").read_bytes()
        self.sig = (FIXTURES / "yt-dlp-SHA2-256SUMS.sig").read_bytes()

    def test_real_release_signature(self):
        verify.verify_pgp(self.sums, self.sig)

    def test_changed_checksums(self):
        forged = self.sums.replace(self.sums[:8], b"00000000", 1)
        self.assertNotEqual(forged, self.sums)
        with self.assertRaises(verify.VerificationError):
            verify.verify_pgp(forged, self.sig)

    def test_corrupted_signature_bytes(self):
        # Header, the signed subpacket area, and the signature itself. The unhashed area that
        # follows the signed one isn't covered by a PGP signature, so it's not tested here.
        for at in (0, 1, 3, 15, 30, len(self.sig) - 1):
            broken = bytearray(self.sig)
            broken[at] ^= 0xFF
            with self.assertRaises(verify.VerificationError, msg=str(at)):
                verify.verify_pgp(self.sums, bytes(broken))

    def test_truncated_signature(self):
        for size in (0, 1, 5, 20, len(self.sig) // 2):
            with self.assertRaises(verify.VerificationError, msg=str(size)):
                verify.verify_pgp(self.sums, self.sig[:size])

    def test_archive_must_match_signed_hash(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"not yt-dlp")
            f.flush()
            with self.assertRaises(verify.VerificationError):
                verify.verify_ytdlp(f.name, "yt-dlp_macos.zip", self.sums, self.sig)

    def test_listing_needs_exactly_one_entry(self):
        hash_ = "a" * 64
        self.assertEqual(verify.sha256_listed(("%s  x.zip\n" % hash_).encode(), "x.zip"), hash_)
        for bad in (b"", ("%s  y.zip\n" % hash_).encode(), ("%s  x.zip\n%s  x.zip\n" % (hash_, hash_)).encode(), b"short  x.zip\n"):
            with self.assertRaises(verify.VerificationError):
                verify.sha256_listed(bad, "x.zip")


class ToolInstall(unittest.TestCase):
    def test_unsigned_binary_never_replaces_the_installed_one(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            existing = bin_dir / "deno"
            existing.write_text("the working copy")

            def fake_curl(url, dest):
                with zipfile.ZipFile(dest, "w") as z:
                    z.writestr("deno", "#!/bin/sh\necho evil\n")

            with mock.patch.object(tools, "BIN_DIR", bin_dir), mock.patch.object(tools, "_curl", fake_curl):
                failed = tools.ensure(lambda _m: None, ["deno"])
            self.assertEqual(failed, ["deno"])
            self.assertEqual(existing.read_text(), "the working copy")

    def test_every_tool_has_a_check(self):
        self.assertEqual(set(tools.SOURCES), set(verify.TEAM_IDS) | {"yt-dlp"})

    def test_downloads_stay_on_https(self):
        self.assertIn("=https", tools._CURL)
        self.assertIn("--proto-redir", tools._CURL)


class ExtraArgs(unittest.TestCase):
    def test_harmless_options_pass(self):
        for text, tokens in (("--no-mtime --write-description", ["--no-mtime", "--write-description"]),
                             ("-S res:1080 --limit-rate=5M", ["-S", "res:1080", "--limit-rate=5M"]),
                             ("--match-filters 'duration>60'", ["--match-filters", "duration>60"]),
                             ("", [])):
            self.assertEqual(settings.split_extra_args(text), (tokens, []), text)

    def test_dangerous_options_refused(self):
        for text in ("--exec calc", "--exec=calc", "--exe calc", "--exec-before-download x",
                     "--use-postprocessor Exec:exec=x", "--plugin-dirs /tmp/p", "--ffmpeg-location /tmp/f",
                     "--config-locations /tmp/c", "--netrc-cmd x", "--cookies-from-browser chrome",
                     "--cookies /tmp/c", "--load-info-json /tmp/j", "--batch-file /tmp/b", "--output /tmp/o",
                     "--paths /tmp/p", "--downloader x", "--downloader-args x", "--postprocessor-args x",
                     "--enable-file-urls", "--print-to-file a b", "--ignore-config", "--", "-o /tmp/x"):
            allowed, refused = settings.split_extra_args(text)
            self.assertTrue(refused, text)
            self.assertFalse(any(t.startswith(text.split()[0].split("=")[0]) for t in allowed), text)

    def test_value_cant_smuggle_an_option(self):
        allowed, refused = settings.split_extra_args("--format --exec calc")
        self.assertNotIn("--exec", allowed)
        self.assertTrue(refused)
        self.assertEqual(settings.split_extra_args("--no-mtime=--exec")[0], [])

    def test_bad_quoting_refused(self):
        self.assertEqual(settings.split_extra_args("--format 'oops")[0], [])

    def test_save_refuses_new_dangerous_value(self):
        with self.assertRaises(ValueError):
            settings.save(dict(settings.DEFAULTS, extra_args="--exec calc"))
        self.assertEqual(settings.load()["extra_args"], "")

    def test_old_value_cant_block_other_settings_but_never_runs(self):
        from noonoo import engine
        settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings.SETTINGS_FILE.write_text(json.dumps({"extra_args": "--exec calc --no-mtime"}))
        try:
            saved = settings.save(settings.load())  # changing something else still works
            self.assertEqual(saved["extra_args"], "--exec calc --no-mtime")
            job = engine.Job.__new__(engine.Job)
            for name, value in (("url", "https://example.com/v"), ("kind", "video"), ("quality", "best"),
                                ("vformat", "h264"), ("aformat", "mp3"), ("folder", ""), ("fps", ""),
                                ("tags", None), ("id", "x"), ("title", "x")):
                setattr(job, name, value)
            job.log = []
            args = engine.build_args(job, saved)
            self.assertNotIn("--exec", args)
            self.assertNotIn("calc", args)
            self.assertIn("--no-mtime", args)
            self.assertTrue(any("--exec" in line for line in job.log))
        finally:
            settings.SETTINGS_FILE.unlink()


class WebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server.App()
        cls.httpd, url = server.serve(cls.app, 0)
        cls.port = cls.httpd.server_address[1]

    def request(self, path, headers=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers={"Host": "127.0.0.1:%d" % self.port, **(headers or {})})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_page_has_a_strict_csp(self):
        r, html = self.request("/?t=" + server.TOKEN)
        self.assertEqual(r.status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-eval", csp)
        script_src = next(p for p in csp.split(";") if "script-src" in p)
        self.assertNotIn("unsafe-inline", script_src)
        nonce = script_src.split("'nonce-")[1].split("'")[0]
        self.assertIn(('<script nonce="%s">' % nonce).encode(), html)
        self.assertNotIn(b"__NONCE__", html)
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_nonce_changes_per_request(self):
        a = self.request("/?t=" + server.TOKEN)[0].getheader("Content-Security-Policy")
        b = self.request("/?t=" + server.TOKEN)[0].getheader("Content-Security-Policy")
        self.assertNotEqual(a, b)

    def test_page_has_no_inline_handlers(self):
        import re
        for name in ("index.html", "app.js"):
            text = (ROOT / "noonoo" / "web" / name).read_text()
            self.assertFalse(re.search(r"<[a-z][^>]*\son(?:error|load|click|change|input|submit)\s*=", text), name)

    def test_dangerous_setting_refused_over_the_api(self):
        r, data = self.request("/api/settings", {"X-Token": server.TOKEN, "Content-Type": "application/json"}, "POST",
                               json.dumps({"settings": {"extra_args": "--exec calc"}}))
        self.assertEqual(r.status, 500)
        self.assertIn("--exec", json.loads(data)["error"])

    def test_guards_still_hold(self):
        self.assertEqual(self.request("/")[0].status, 403)
        self.assertEqual(self.request("/?t=" + server.TOKEN, {"Host": "evil.example"})[0].status, 403)
        self.assertEqual(self.request("/api/settings")[0].status, 403)
        self.assertEqual(self.request("/static/../server.py")[0].status, 404)
        self.assertEqual(self.request("/static/%2e%2e/server.py")[0].status, 404)


class DataFolder(unittest.TestCase):
    def test_private(self):
        tools.secure_data_dir()
        self.assertEqual(tools.DATA_DIR.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main(verbosity=1)
