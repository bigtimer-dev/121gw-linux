"""Newer custom firmware for the meter: the releases of bigtimer-dev/121GW-Open-Firmware (tags custom-cA.BC).

Only meters that already run a custom build ("C-A.BC") are offered one; zombu4's own builds (F-...) and EEVblog's v2.05
are left alone. A release carries EEVBlog.bin and SHA256SUMS; the download is accepted only when its SHA-256 matches,
the image passes check_image and the version inside it is the release's. Standard library only (urllib).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
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
