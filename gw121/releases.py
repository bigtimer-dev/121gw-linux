"""Newer custom firmware for the meter: the releases of bigtimer-dev/121GW-Open-Firmware (tags custom-cA.BC).

Only meters that already run a custom build ("C-A.BC") are offered one; zombu4's own builds (F-...) and EEVblog's v2.05
are left alone. A release carries EEVBlog.bin and SHA256SUMS; the download is accepted only when its SHA-256 matches,
the image passes check_image and the version inside it is the release's. Standard library only (urllib).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .upload import check_image, image_version

REPO = "bigtimer-dev/121GW-Open-Firmware"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
USER_AGENT = "121gw-linux"
IMAGE_NAME = "EEVBlog.bin"
SUMS_NAME = "SHA256SUMS"
TIMEOUT = 15

_TAG = re.compile(r"^custom-c(\d+)\.(\d+)$")
_VERSION = re.compile(r"^C-(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Release:
    version: str            # "C-1.04"
    tag: str                # "custom-c1.04"
    image_url: str
    sums_url: str
    page_url: str


def version_key(version: str | None) -> tuple[int, int] | None:
    m = _VERSION.match(version or "")
    return (int(m[1]), int(m[2])) if m else None


def parse_releases(items: list) -> Release | None:
    """The newest published custom release that carries the image and its checksums."""
    best, best_key = None, None
    for r in items:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        m = _TAG.match(r.get("tag_name") or "")
        if not m:
            continue
        assets = {a.get("name"): a.get("browser_download_url") for a in r.get("assets") or [] if isinstance(a, dict)}
        if not assets.get(IMAGE_NAME) or not assets.get(SUMS_NAME):
            continue
        key = (int(m[1]), int(m[2]))
        if best_key is None or key > best_key:
            best_key = key
            best = Release(f"C-{m[1]}.{m[2]}", r["tag_name"], assets[IMAGE_NAME], assets[SUMS_NAME],
                           r.get("html_url") or "")
    return best


def is_newer(release: Release | None, meter_version: str | None) -> bool:
    """True when the meter runs an older custom build than the release."""
    have, new = version_key(meter_version), version_key(release.version if release else None)
    return have is not None and new is not None and new > have


def _get(url: str, accept: str = "application/octet-stream") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_latest() -> Release | None:
    data = _get(API, "application/vnd.github+json")
    return parse_releases(json.loads(data))


def verify_image(release: Release, image: bytes, sums: bytes) -> str | None:
    """None when the image is the release's, else why not."""
    want = None
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").split("/")[-1] == IMAGE_NAME:
            want = parts[0].lower()
    if want is None:
        return f"{SUMS_NAME} has no line for {IMAGE_NAME}"
    if hashlib.sha256(image).hexdigest() != want:
        return "the downloaded file does not match its SHA-256"
    bad = check_image(image)
    if bad:
        return bad
    if image_version(image) != release.version:
        return f"the image says {image_version(image) or 'no version'}, the release {release.version}"
    return None


def download(release: Release, cache_dir: Path | None = None) -> bytes:
    """The release's image, checked; kept in cache_dir as <version>.bin. Raises ValueError when a check fails."""
    if cache_dir is not None:
        cached = cache_dir / f"{release.version}.bin"
        if cached.is_file():
            data = cached.read_bytes()
            if check_image(data) is None and image_version(data) == release.version:
                sums = _get(release.sums_url)
                if verify_image(release, data, sums) is None:
                    return data
    image, sums = _get(release.image_url), _get(release.sums_url)
    why = verify_image(release, image, sums)
    if why:
        raise ValueError(why)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{release.version}.bin").write_bytes(image)
    return image


# ---- zombu4's two-part bootloader (releases bl-vA.B.C of zombu4/121GW-Open-Firmware) ---------------------------

BL_REPO = "zombu4/121GW-Open-Firmware"
BL_API = f"https://api.github.com/repos/{BL_REPO}/releases?per_page=50"
_BL_TAG = re.compile(r"^bl-v(\d+)\.(\d+)\.(\d+)$")
_BL_METER = re.compile(r"^B-(\d)\.(\d)(\d)$")
BL_HEADER_MAGIC = 0x32484C42                # "BLH2", the slot header after the starter in install.bin


@dataclass(frozen=True)
class BootloaderRelease:
    version: tuple[int, int, int]           # (1, 1, 2)
    tag: str                                # "bl-v1.1.2"
    zip_url: str
    sha_url: str
    page_url: str

    @property
    def meter_text(self) -> str:            # "B-1.12", as the meter reports it
        a, b, c = self.version
        return f"B-{a}.{b}{c}"

    @property
    def header_version(self) -> int:        # sent with the image in the bootloader's START
        a, b, c = self.version
        return a << 16 | b << 8 | c


def bootloader_key(text: str | None) -> tuple[int, int, int] | None:
    """(A, B, C) of the meter's "B-A.BC"; None for EEVblog's ("B-EEV") or unknown."""
    m = _BL_METER.match(text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def parse_bootloader_releases(items: list) -> BootloaderRelease | None:
    best = None
    for r in items:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        m = _BL_TAG.match(r.get("tag_name") or "")
        if not m:
            continue
        zname = f"bootloader-{r['tag_name']}.zip"
        assets = {a.get("name"): a.get("browser_download_url") for a in r.get("assets") or [] if isinstance(a, dict)}
        if not assets.get(zname) or not assets.get(zname + ".sha256"):
            continue
        v = (int(m[1]), int(m[2]), int(m[3]))
        if best is None or v > best.version:
            best = BootloaderRelease(v, r["tag_name"], assets[zname], assets[zname + ".sha256"], r.get("html_url") or "")
    return best


def fetch_latest_bootloader() -> BootloaderRelease | None:
    return parse_bootloader_releases(json.loads(_get(BL_API, "application/vnd.github+json")))


def bootloader_is_newer(release: BootloaderRelease | None, meter_bootloader: str | None) -> bool:
    have = bootloader_key(meter_bootloader)
    return release is not None and have is not None and release.version > have


def check_install_file(data: bytes) -> str | None:
    """The checks the firmware makes before a first install (ble/bft.s): None or why not."""
    if not 0x580 <= len(data) <= 0x6000 or len(data) % 128:
        return f"install file of {len(data)} bytes: 0x580 .. 0x6000 in whole half-pages expected"
    sp, reset = struct.unpack_from("<II", data, 0)
    if not 0x20000001 <= sp <= 0x2000C000 or not reset & 1 or not 0x08000000 <= reset < 0x08000400:
        return "the starter part has no valid vector table"
    if struct.unpack_from("<I", data, 0x400)[0] != BL_HEADER_MAGIC:
        return "no bootloader header after the starter"
    return None


def check_bootloader_zip(release: BootloaderRelease, blob: bytes, sha_text: bytes) -> tuple[bytes, bytes]:
    """(install.bin, blimage.bin) out of the release zip; ValueError when any check fails: the zip's published SHA-256,
    every file against SHA256SUMS inside the zip, the firmware's install-file checks, and the same bootloader in both."""
    want = (sha_text.decode("ascii", "replace").split() or [""])[0].lower()
    if hashlib.sha256(blob).hexdigest() != want:
        raise ValueError(f"{release.tag}: the zip does not match its published SHA-256")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        if not {"install.bin", "blimage.bin", "SHA256SUMS"} <= names:
            raise ValueError(f"{release.tag}: the zip lacks install.bin, blimage.bin or SHA256SUMS")
        files = {n: z.read(n) for n in ("install.bin", "blimage.bin")}
        sums = z.read("SHA256SUMS").decode("utf-8", "replace")
    listed = {}
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2:
            listed[parts[1].lstrip("*").split("/")[-1]] = parts[0].lower()
    for name, data in files.items():
        if listed.get(name) != hashlib.sha256(data).hexdigest():
            raise ValueError(f"{release.tag}: {name} does not match SHA256SUMS")
    inst, boot = files["install.bin"], files["blimage.bin"]
    bad = check_install_file(inst)
    if bad:
        raise ValueError(f"{release.tag}: {bad}")
    if not boot or inst[0x500:0x500 + len(boot)] != boot:
        raise ValueError(f"{release.tag}: install.bin and blimage.bin hold different bootloaders")
    return inst, boot


def download_bootloader(release: BootloaderRelease, cache_dir: Path | None = None) -> tuple[bytes, bytes]:
    """(install.bin, blimage.bin) of the release, checked; the zip is kept in cache_dir."""
    sha = _get(release.sha_url)
    if cache_dir is not None:
        cached = cache_dir / f"bootloader-{release.tag}.zip"
        if cached.is_file():
            try:
                return check_bootloader_zip(release, cached.read_bytes(), sha)
            except (ValueError, zipfile.BadZipFile):
                pass
    blob = _get(release.zip_url)
    try:
        files = check_bootloader_zip(release, blob, sha)
    except zipfile.BadZipFile as e:
        raise ValueError(f"{release.tag}: {e}") from e
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"bootloader-{release.tag}.zip").write_bytes(blob)
    return files
