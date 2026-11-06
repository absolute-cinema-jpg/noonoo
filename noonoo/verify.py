"""Proves that NooNoo and the tools it runs came from their publishers, not just from a URL.

Each check is independent of where the file was downloaded from, so taking over a download
host or a GitHub release isn't enough on its own to get code running here:

    NooNoo updates    ECDSA signature, checked with the public key shipped in the app.
                      The private key never leaves the release machine.
    deno, ffmpeg,     Apple Developer ID signature from the publisher's own team.
    ffprobe
    yt-dlp            SHA-256 listed in the checksum file yt-dlp signs with its PGP key.

All of these fail closed: if a check can't run, nothing is installed. macOS ships no PGP
tool, so the small part of OpenPGP needed for yt-dlp's signature (v4, RSA) is implemented here.
"""
import hashlib
import subprocess
from pathlib import Path

CODESIGN = "/usr/bin/codesign"
OPENSSL = "/usr/bin/openssl"
UPDATE_KEY = Path(__file__).with_name("update_key.pem")

TEAM_IDS = {"deno": "2H4KBF436B", "ffmpeg": "KU3N25YGLU", "ffprobe": "KU3N25YGLU"}

# yt-dlp's release key, from public.key in its repository.
# Fingerprint AC0C BBE6 848D 6A87 3464  AF4E 57CF 6593 3B5A 7581
YTDLP_KEY_N = int(
    "f4ac5f738c63c0b74b6196de42d5e6f371c0155fb35bd6ff9ea908e4f96119753113542837580339"
    "9e002b947766fdf66acd480efa9018ac1eb418a7a0edf7d40dff92d1714a11975e44b61e01aaaa2a"
    "14a26d56a50e3bce04ecedbb75bad3b1cb113400a80ef04cfb88b765aa9a92fad0d21ccabdbb2f1a"
    "a681d37798dbd9da22cef9b3f49ca47f0be3e013d1f8cadcb00dad76cccc7a3e66429d538e3aee97"
    "644166754c71750f5f143a9df4b701298d8b0ce712628d6c698d185a26561edcd5a19bb590ece39d"
    "2e458eaff204724b2260c7ee93711e6eecfcf9dababa66405b4316bba4b7eda58439e9294986aff2"
    "3fdc81ea1ff5fef673e84b8ba2a90e47a5b941f012bee167e96a2120ef92e7bee38f7d920f298c97"
    "6bfd00ed0a2333080eac3f3ca334fe54290a92427e8a35d9ee01cd8e670169474a2bb7c320439823"
    "adcbc9ce905a41904e28057df4945d61c308ef5a5cd84fdde8ea15680632d094b2b6e4709916b966"
    "3bab9011cbfd391db48cdcae0353d7aab67c66895d5643c9d7b88f49809411be1e4f4e49f2e4bb71"
    "ab3726aaf58ef538c705baecd7804f10ef5f717c4a2ff08505bcd70d2bef02002b76aaa53289dc43"
    "fca271395f2faab327c10a162a96d7840c4dd027e92bae653a1d5c24e2227c284fb5a38ee14f28a7"
    "6f7c7d463f86e7a9dcd39f32f10920c812d7f27cfb085fef4623b26493fcd98d", 16)
YTDLP_KEY_E = 65537

_DIGEST_INFO = {  # OpenPGP hash algorithm -> (hash name, DER prefix from RFC 8017)
    8: ("sha256", bytes.fromhex("3031300d060960864801650304020105000420")),
    10: ("sha512", bytes.fromhex("3051300d060960864801650304020305000440")),
}


class VerificationError(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---- NooNoo updates ---------------------------------------------------------

def verify_update(archive, signature, key=UPDATE_KEY):
    """Raises VerificationError unless `signature` is the release key's over `archive`."""
    if not Path(key).is_file():
        raise VerificationError("this copy of NooNoo has no update key")
    r = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", str(key), "-signature", str(signature), str(archive)],
                       capture_output=True, text=True)
    if r.returncode != 0 or "Verified OK" not in r.stdout:
        raise VerificationError("the update's signature is not valid")


# ---- Developer ID tools -----------------------------------------------------

def verify_team(binary, name):
    """Raises VerificationError unless `binary` is signed by `name`'s Developer ID team."""
    team = TEAM_IDS[name]
    requirement = 'anchor apple generic and certificate leaf[subject.OU] = "%s"' % team
    r = subprocess.run([CODESIGN, "--verify", "--strict", "-R=" + requirement, str(binary)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise VerificationError("%s is not signed by its publisher (team %s)" % (name, team))


# ---- yt-dlp -----------------------------------------------------------------

def _packets(data):
    """(tag, body) for each OpenPGP packet, old or new format."""
    i, n = 0, len(data)
    while i < n:
        head = data[i]
        i += 1
        if not head & 0x80:
            raise VerificationError("malformed signature")
        if head & 0x40:  # new format
            tag = head & 0x3F
            first = data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                length = ((first - 192) << 8) + data[i] + 192
                i += 1
            elif first == 255:
                length = int.from_bytes(data[i:i + 4], "big")
                i += 4
            else:
                raise VerificationError("unsupported signature encoding")
        else:  # old format
            tag = (head >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(head & 3)
            if size is None:
                raise VerificationError("unsupported signature encoding")
            length = int.from_bytes(data[i:i + size], "big")
            i += size
        if i + length > n:
            raise VerificationError("truncated signature")
        yield tag, data[i:i + length]
        i += length


def verify_pgp(data, signature, n=None, e=None):
    """Raises VerificationError unless `signature` is a valid detached v4 RSA signature
    over `data` by the given key (yt-dlp's by default)."""
    n, e = n or YTDLP_KEY_N, e or YTDLP_KEY_E
    try:
        body = next(b for tag, b in _packets(signature) if tag == 2)
        version, sig_type, pub_algo, hash_algo = body[0], body[1], body[2], body[3]
        if version != 4 or sig_type != 0 or pub_algo != 1 or hash_algo not in _DIGEST_INFO:
            raise VerificationError("unsupported signature type")
        hashed_len = int.from_bytes(body[4:6], "big")
        signed_part = body[:6 + hashed_len]
        at = 6 + hashed_len
        unhashed_len = int.from_bytes(body[at:at + 2], "big")
        at += 2 + unhashed_len
        left16 = body[at:at + 2]
        bits = int.from_bytes(body[at + 2:at + 4], "big")
        mpi = body[at + 4:at + 4 + (bits + 7) // 8]
    except (StopIteration, IndexError) as exc:
        raise VerificationError("malformed signature") from exc

    name, prefix = _DIGEST_INFO[hash_algo]
    h = hashlib.new(name)
    h.update(data)
    h.update(signed_part)
    h.update(b"\x04\xff" + len(signed_part).to_bytes(4, "big"))
    digest = h.digest()
    if digest[:2] != left16:
        raise VerificationError("the signature doesn't match the checksums")

    size = (n.bit_length() + 7) // 8
    decoded = pow(int.from_bytes(mpi, "big"), e, n).to_bytes(size, "big")
    tail = prefix + digest
    expected = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    if decoded != expected:
        raise VerificationError("the signature doesn't match the checksums")


def sha256_listed(sums, filename):
    """The hash for `filename` in a signed checksum file. Exactly one entry must match."""
    found = []
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            found.append(parts[0].lower())
    if len(found) != 1 or len(found[0]) != 64:
        raise VerificationError("%s isn't listed in the signed checksums" % filename)
    return found[0]


def verify_ytdlp(archive, filename, sums, signature):
    """Raises VerificationError unless `archive` is the file yt-dlp signed for as `filename`."""
    verify_pgp(sums, signature)
    if sha256_file(archive) != sha256_listed(sums, filename):
        raise VerificationError("%s doesn't match yt-dlp's signed checksum" % filename)
