import hashlib
import struct
import unittest

from gw121 import releases as R


def image(version_chars: bytes = b"C-104") -> bytes:
    """A valid image header plus the version answer table the firmware keeps (F6 c0..c4 xor)."""
    x = 0xF6
    for c in version_chars:
        x ^= c
    body = bytes((i * 7 + 3) & 0xFF for i in range(4000)) + b"\xF6" + version_chars + bytes([x])
    return struct.pack("<II", 0x20014000, 0x08006101) + body


def rel(tag, assets=(R.IMAGE_NAME, R.SUMS_NAME), **kw):
    d = {"tag_name": tag, "html_url": "https://example/" + tag,
         "assets": [{"name": n, "browser_download_url": f"https://example/{tag}/{n}"} for n in assets]}
    d.update(kw)
    return d


class ReleaseTests(unittest.TestCase):
    def test_newest_custom_release_with_its_files(self):
        items = [rel("custom-c1.03"), rel("custom-c1.10", draft=True), rel("v1.0.4"), rel("custom-c1.04"),
                 rel("custom-c1.05", assets=(R.IMAGE_NAME,)), rel("custom-c1.06", prerelease=True)]
        r = R.parse_releases(items)
        self.assertEqual((r.version, r.tag), ("C-1.04", "custom-c1.04"))
        self.assertTrue(r.image_url.endswith("/EEVBlog.bin"))

    def test_no_release(self):
        self.assertIsNone(R.parse_releases([]))
        self.assertIsNone(R.parse_releases([rel("v1.0.4")]))

    def test_only_older_custom_builds_are_offered(self):
        r = R.parse_releases([rel("custom-c1.04")])
        self.assertTrue(R.is_newer(r, "C-1.03"))
        self.assertTrue(R.is_newer(r, "C-0.96"))
        self.assertFalse(R.is_newer(r, "C-1.04"))
        self.assertFalse(R.is_newer(r, "C-1.05"))
        self.assertFalse(R.is_newer(r, "F-1.04"))          # zombu4's own build: left alone
        self.assertFalse(R.is_newer(r, None))              # v2.05 / unknown
        self.assertFalse(R.is_newer(None, "C-1.03"))

    def test_verify_image(self):
        r = R.parse_releases([rel("custom-c1.04")])
        img = image()
        sums = f"{hashlib.sha256(img).hexdigest()}  EEVBlog.bin\n".encode()
        self.assertIsNone(R.verify_image(r, img, sums))
        self.assertIsNone(R.verify_image(r, img, f"{hashlib.sha256(img).hexdigest()}  2_fast/EEVBlog.bin\n".encode()))
        self.assertIn("SHA-256", R.verify_image(r, img[:-1] + b"\x00", sums))
        self.assertIn("no line", R.verify_image(r, img, b"00  other.bin\n"))
        other = image(b"C-103")
        self.assertIn("C-1.03", R.verify_image(r, other, f"{hashlib.sha256(other).hexdigest()}  EEVBlog.bin".encode()))


if __name__ == "__main__":
    unittest.main()
