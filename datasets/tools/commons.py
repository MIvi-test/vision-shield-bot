"""Wikimedia Commons client and selection rules for dataset candidates.

Only the standard library is used. Limits come from the media profile, so
dataset images also pass the real media pipeline (docs/policy.md §2).
"""

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

REPO_ROOT = Path(__file__).resolve().parents[2]
MEDIA_PROFILE = (
    REPO_ROOT / "packages" / "contracts" / "policies" / "media-profile-v0.json"
)

API_URL = "https://commons.wikimedia.org/w/api.php"
FILE_HOST = "upload.wikimedia.org"
PAGE_PREFIX = "https://commons.wikimedia.org/wiki/"
# Wikimedia asks automated clients for a descriptive User-Agent with contact info.
USER_AGENT = (
    "vision-shield-bot-dataset/0.1 "
    "(student project; https://github.com/MIvi-test/vision-shield-bot)"
)
REQUEST_TIMEOUT_S = 60
API_PAUSE_S = 1.0
CHUNK_BYTES = 64 * 1024
DOWNLOAD_ATTEMPTS = 3
RETRY_PAUSE_S = 5.0

FORMAT_MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
MIME_EXTENSION = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

# Commons short license names -> SPDX ids allowed by the manifest schema.
CC_LICENSE = re.compile(r"^CC BY(?P<sa>-SA)? (?P<version>1\.0|2\.0|2\.5|3\.0|4\.0)$")
OTHER_LICENSES = {
    "CC0": "CC0-1.0",
    "Public domain": "PDM-1.0",
}


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int
    max_pixels: int
    max_side_px: int
    min_side_px: int
    mime_types: frozenset[str]


@dataclass(frozen=True)
class Candidate:
    title: str
    source_url: str
    file_url: str
    sha1: str
    size: int
    width: int
    height: int
    mime: str
    license: str
    author: str

    @property
    def extension(self) -> str:
        return MIME_EXTENSION[self.mime]


def load_limits(path: Path = MEDIA_PROFILE) -> Limits:
    profile = json.loads(path.read_text(encoding="utf-8"))["input"]
    return Limits(
        max_file_bytes=profile["max_file_bytes"],
        max_pixels=profile["max_pixels"],
        max_side_px=profile["max_side_px"],
        min_side_px=profile["min_side_px"],
        mime_types=frozenset(FORMAT_MIME[f] for f in profile["formats"]),
    )


def spdx_license(short_name: str) -> str | None:
    """Map a Commons LicenseShortName to SPDX; None for anything not allowed."""
    name = short_name.strip()
    if name in OTHER_LICENSES:
        return OTHER_LICENSES[name]
    match = CC_LICENSE.match(name)
    if match is None:
        return None
    return f"CC-BY{match['sa'] or ''}-{match['version']}"


def plain_text(value: str) -> str:
    """Strip HTML tags and entities from Commons metadata, collapse spaces."""
    text = re.sub(r"<[^>]*>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def without_query(url: str, host: str) -> str:
    """Drop query and fragment; refuse anything that is not https on host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != host:
        raise ValueError(f"unexpected URL host: {parts.scheme}://{parts.hostname}")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def metadata_value(info: dict[str, Any], key: str) -> str:
    entry = info.get("extmetadata", {}).get(key) or {}
    value = entry.get("value", "")
    return value if isinstance(value, str) else str(value)


def evaluate(page: dict[str, Any], limits: Limits) -> tuple[Candidate | None, str]:
    """Return a candidate or the reason the file is skipped."""
    infos = page.get("imageinfo") or []
    if page.get("missing") or not infos:
        return None, "no file info"
    info = infos[0]
    mime = info.get("mime", "")
    if mime not in limits.mime_types:
        return None, f"format {mime or 'unknown'}"
    size, width, height = info["size"], info["width"], info["height"]
    if size > limits.max_file_bytes:
        return None, "file too large"
    if width * height > limits.max_pixels or max(width, height) > limits.max_side_px:
        return None, "too many pixels"
    if min(width, height) < limits.min_side_px:
        return None, "too small"
    restrictions = metadata_value(info, "Restrictions").lower()
    if "personality" in restrictions:
        return None, "identifiable people"
    license_name = plain_text(metadata_value(info, "LicenseShortName"))
    license_id = spdx_license(license_name)
    if license_id is None:
        return None, f"license {license_name or 'unknown'}"
    author = plain_text(metadata_value(info, "Artist"))
    if not author:
        return None, "no author"
    try:
        file_url = without_query(info["url"], FILE_HOST)
        source_url = without_query(info["descriptionurl"], "commons.wikimedia.org")
    except (KeyError, ValueError) as error:
        return None, f"bad url: {error}"
    if not source_url.startswith(PAGE_PREFIX + "File:"):
        return None, "bad url: not a file page"
    return (
        Candidate(
            title=page["title"],
            source_url=source_url,
            file_url=file_url,
            sha1=info["sha1"],
            size=size,
            width=width,
            height=height,
            mime=mime,
            license=license_id,
            author=author[:200],
        ),
        "ok",
    )


def open_url(url: str) -> BinaryIO:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S)


def api_query(params: dict[str, str], opener: Callable[[str], BinaryIO] = open_url):
    query = {"action": "query", "format": "json", "formatversion": "2", **params}
    with opener(f"{API_URL}?{urllib.parse.urlencode(query)}") as response:
        return json.load(response)


def category_files(
    category: str,
    opener: Callable[[str], BinaryIO] = open_url,
    pause_s: float = API_PAUSE_S,
) -> Iterator[dict[str, Any]]:
    """Yield file pages of a category with image info, following continuation."""
    params = {
        "generator": "categorymembers",
        "gcmtitle": f"Category:{category}",
        "gcmtype": "file",
        "gcmlimit": "50",
        "prop": "imageinfo",
        "iiprop": "url|sha1|size|mime|extmetadata",
        "iiextmetadatafilter": "LicenseShortName|Artist|Restrictions",
    }
    while True:
        data = api_query(params, opener)
        yield from data.get("query", {}).get("pages", [])
        if "continue" not in data:
            return
        params = {**params, **data["continue"]}
        time.sleep(pause_s)


class HashMismatch(ValueError):
    """Downloaded bytes differ from the expected hash."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_file(
    url: str,
    destination: Path,
    max_bytes: int,
    *,
    expected_sha1: str | None = None,
    expected_sha256: str | None = None,
    opener: Callable[[str], BinaryIO] = open_url,
) -> str:
    """Stream url to destination, check the expected hashes, return SHA-256.

    Only FILE_HOST is allowed. The file is written to a temporary name first and
    renamed only after all checks pass, so an interrupted or rejected download
    leaves nothing behind.
    """
    if urllib.parse.urlsplit(url).hostname != FILE_HOST:
        raise ValueError(f"refusing to download from {url}")
    temporary = destination.with_name(destination.name + ".part")
    sha1, sha256, received = hashlib.sha1(), hashlib.sha256(), 0
    try:
        with opener(url) as response, temporary.open("wb") as out:
            while chunk := response.read(CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError(f"file exceeds {max_bytes} bytes")
                sha1.update(chunk)
                sha256.update(chunk)
                out.write(chunk)
        if expected_sha1 is not None and sha1.hexdigest() != expected_sha1:
            raise HashMismatch("SHA-1 does not match Commons metadata")
        if expected_sha256 is not None and sha256.hexdigest() != expected_sha256:
            raise HashMismatch(
                f"SHA-256 expected {expected_sha256}, got {sha256.hexdigest()}"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256.hexdigest()


def with_retry[T](
    action: Callable[[], T],
    attempts: int = DOWNLOAD_ATTEMPTS,
    pause_s: float = RETRY_PAUSE_S,
) -> T:
    """Repeat action after network errors (OSError) only.

    A ValueError means the file itself is wrong (size, hash, host); repeating
    the request would not change that, so it is raised at once.
    """
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(pause_s * attempt)
    raise AssertionError("unreachable")


def download(
    candidate: Candidate,
    destination: Path,
    max_bytes: int,
    opener: Callable[[str], BinaryIO] = open_url,
) -> str:
    """Download a Commons candidate, check SHA-1 from the API, return SHA-256."""
    return fetch_file(
        candidate.file_url,
        destination,
        max_bytes,
        expected_sha1=candidate.sha1,
        opener=opener,
    )


def download_with_retry(
    candidate: Candidate,
    destination: Path,
    max_bytes: int,
    opener: Callable[[str], BinaryIO] = open_url,
    attempts: int = DOWNLOAD_ATTEMPTS,
    pause_s: float = RETRY_PAUSE_S,
) -> str:
    return with_retry(
        lambda: download(candidate, destination, max_bytes, opener),
        attempts,
        pause_s,
    )
