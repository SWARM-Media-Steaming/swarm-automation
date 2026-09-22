"""Download images pasted onto a GitHub issue and attach them to an AI prompt.

GitHub stores a pasted screenshot as a `user-attachments` URL inside the issue
or comment Markdown. The interactive CLIs accept that same picture as a real
image (Codex `--image`, Claude stream-json content, Grok `--prompt-json`).
Text-only prompts drop the picture, so grading and implementation both miss it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

# Enough for several UI screenshots. Further images stay in the issue text.
MAX_IMAGES = 8
# Reject anything larger before writing it. GitHub's own upload cap is higher;
# an 8 MiB picture is already too big to be a useful model attachment.
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
# Shrink before inlining so a screenshot fits in a CLI argv or stdin budget.
INLINE_TARGET_BYTES = 300_000
# `--prompt-json` is one argv entry. Stay under the macOS ARG_MAX (~1 MiB)
# with room for the rest of the command line.
GROK_PROMPT_JSON_BUDGET = 700_000
# Claude receives the image on stdin, so the cap is the request size, not argv.
CLAUDE_STDIN_BUDGET = 8_000_000

_MEDIA_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MARKDOWN_IMAGE = re.compile(
    r"!\[([^\]]*)\]\(\s*<?(https://[^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)",
    re.IGNORECASE,
)
_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_SRC_ATTR = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""",
    re.IGNORECASE,
)
_ALT_ATTR = re.compile(
    r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
_BARE_URL = re.compile(r"https://[^\s<>()]+", re.IGNORECASE)


class ImageDownloadError(Exception):
    """One issue image could not be saved. The worker keeps going."""


@dataclass(frozen=True)
class IssueImage:
    url: str
    path: Path
    media_type: str
    alt: str
    source: str


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Leave 3xx responses intact so Authorization is not forwarded to a CDN."""

    def http_response(self, request, response):  # type: ignore[no-untyped-def]
        return response

    https_response = http_response


_OPENER = urllib.request.build_opener(_NoRedirect)


def _clean_url(value: str) -> str:
    return html.unescape(value).strip().rstrip(".,);>]\"'")


def _clean_alt(value: str) -> str:
    collapsed = " ".join(value.replace("\n", " ").split())
    return collapsed[:120]


def _first_group(match: object) -> str:
    groups = getattr(match, "groups")()
    return next((group for group in groups if group), "")


def _image_suffix(path: str) -> str:
    return Path(urllib.parse.urlparse(path).path).suffix.lower()


def _github_attachment_path(path: str) -> bool:
    lowered = path.lower()
    if "/user-attachments/assets/" in lowered:
        return True
    parts = [part for part in path.split("/") if part]
    # /<owner>/<repo>/assets/<id>/<file> — GitHub's older issue upload path.
    return len(parts) >= 5 and parts[2] == "assets" and parts[3].isdigit()


def is_issue_image_url(url: str, github_host: str = "github.com") -> bool:
    """True for a GitHub-hosted picture a person can paste into an issue.

    Other hosts are ignored so an issue body cannot make the worker fetch
    arbitrary internal URLs.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    configured = github_host.strip().lower() or "github.com"
    path = parsed.path or ""
    suffix = _image_suffix(path)
    if suffix in {".svg", ".mp4", ".mov", ".webm", ".pdf"}:
        return False
    github_pages = {configured, "github.com", "www.github.com"}
    if host in github_pages or host.endswith(".ghe.com"):
        return _github_attachment_path(path) or suffix in _IMAGE_SUFFIXES
    if host in {"camo.githubusercontent.com", "user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}:
        return True
    if host.endswith(".githubusercontent.com"):
        return suffix in _IMAGE_SUFFIXES or _github_attachment_path(path)
    return False


def display_image_url(url: str) -> str:
    """URL without a signed query string, safe to write into a log or prompt."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


def extract_issue_image_refs(text: str, *, github_host: str = "github.com") -> list[tuple[str, str]]:
    """`(url, alt)` in reading order. The same URL is returned once."""
    found: list[tuple[int, str, str]] = []
    for match in _MARKDOWN_IMAGE.finditer(text or ""):
        url = _clean_url(match.group(2))
        if is_issue_image_url(url, github_host):
            found.append((match.start(), url, _clean_alt(match.group(1))))
    for match in _IMG_TAG.finditer(text or ""):
        attrs = match.group(1)
        src = _SRC_ATTR.search(attrs)
        if src is None:
            continue
        url = _clean_url(_first_group(src))
        alt_match = _ALT_ATTR.search(attrs)
        alt = _clean_alt(_first_group(alt_match)) if alt_match else ""
        if is_issue_image_url(url, github_host):
            found.append((match.start(), url, alt))
    for match in _BARE_URL.finditer(text or ""):
        url = _clean_url(match.group(0))
        if is_issue_image_url(url, github_host):
            found.append((match.start(), url, ""))
    found.sort(key=lambda item: item[0])
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for _, url, alt in found:
        if url in seen:
            continue
        seen.add(url)
        ordered.append((url, alt))
    return ordered


def _is_loopback(hostname: str) -> bool:
    name = hostname.lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _host_allowed(hostname: str, *, loopback_ok: bool) -> bool:
    name = hostname.lower().rstrip(".")
    if not name:
        return False
    if _is_loopback(name):
        return loopback_ok
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return True
    return address.is_global


def fetch_image_bytes(
    url: str,
    token: str = "",
    *,
    timeout: float = 30,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[bytes, str]:
    """GET an image, following redirects without sending the GitHub token to the CDN.

    A signed `private-user-images` redirect rejects (or leaks) the bearer token
    GitHub needs on the first `github.com` hop. The token stays on the original
    host only. Loopback HTTP is allowed so tests can serve a fixture; a public
    GitHub URL cannot be redirected there.
    """
    client = opener or _OPENER
    current = url
    origin = urllib.parse.urlparse(url)
    origin_host = (origin.hostname or "").lower()
    loopback_ok = _is_loopback(origin_host)
    for _ in range(6):
        parsed = urllib.parse.urlparse(current)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"https", "http"}:
            raise ImageDownloadError("image URL scheme is not allowed")
        if scheme != "https" and not (loopback_ok and _is_loopback(host)):
            raise ImageDownloadError("image URL must use https")
        if not _host_allowed(host, loopback_ok=loopback_ok):
            raise ImageDownloadError("image URL host is not allowed")
        headers = {
            "User-Agent": "swarm-issue-worker",
            "Accept": "application/octet-stream, image/png, image/jpeg, image/gif, image/webp, */*",
        }
        if token and host == origin_host:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(current, headers=headers, method="GET")
        try:
            response = client.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise ImageDownloadError(f"HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise ImageDownloadError(f"request failed: {error.reason}") from error
        with response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ImageDownloadError(f"HTTP {status} without a redirect target")
                current = urllib.parse.urljoin(current, location)
                continue
            if status != 200:
                raise ImageDownloadError(f"HTTP {status}")
            data = response.read(max_bytes + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(data) > max_bytes:
            raise ImageDownloadError("image is larger than 8 MiB")
        return data, content_type
    raise ImageDownloadError("too many redirects")


def sniff_media_type(data: bytes, content_type: str = "") -> str | None:
    """Image type from magic bytes. HTML and other payloads are rejected."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    lowered = content_type.split(";", 1)[0].strip().lower()
    if lowered.startswith("text/") or lowered in {"application/json", "application/xml"}:
        return None
    return None


def download_issue_image(
    url: str,
    alt: str,
    source: str,
    dest_dir: Path,
    *,
    token: str = "",
    timeout: float = 30,
    fetch: Callable[..., tuple[bytes, str]] = fetch_image_bytes,
) -> IssueImage:
    dest_dir.mkdir(parents=True, exist_ok=True)
    data, content_type = fetch(url, token, timeout=timeout)
    media = sniff_media_type(data, content_type)
    if media is None:
        raise ImageDownloadError(f"{display_image_url(url)} was not a PNG, JPEG, GIF, or WebP image")
    filename = hashlib.sha256(url.encode()).hexdigest()[:20] + _MEDIA_EXTENSIONS[media]
    path = dest_dir / filename
    path.write_bytes(data)
    return IssueImage(url=url, path=path, media_type=media, alt=alt, source=source)


def format_image_note(images: Sequence[IssueImage]) -> str:
    """Tell the model which attached image is which. Empty when there are none."""
    if not images:
        return ""
    lines = [
        "Issue images:",
        "These images were uploaded to the GitHub issue and are attached to this prompt "
        "the same way an image pasted into the CLI is attached. Image 1 is the first "
        "attachment. Use what they show as part of the request. If an attachment is not "
        "visible in the message, read its local file before planning or editing.",
    ]
    for index, image in enumerate(images, start=1):
        label = image.alt.strip() or "untitled"
        lines.append(
            f"- Image {index} ({label}) from {image.source}: local file `{image.path}` "
            f"(original {display_image_url(image.url)})"
        )
    return "\n".join(lines)


def shrink_image_bytes(path: Path, target_bytes: int = INLINE_TARGET_BYTES) -> bytes | None:
    """Recompress with macOS `sips` until the file is small enough to inline."""
    sips = shutil.which("sips")
    if sips is None:
        return None
    with tempfile.TemporaryDirectory(prefix="swarm-image-") as temporary:
        dest = Path(temporary) / "shrunk.jpg"
        for edge, quality in ((1600, 60), (1280, 50), (1024, 40), (800, 30)):
            dest.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    sips,
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    str(quality),
                    "-Z",
                    str(edge),
                    str(path),
                    "--out",
                    str(dest),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and dest.is_file() and dest.stat().st_size <= target_bytes:
                data = dest.read_bytes()
                if sniff_media_type(data) == "image/jpeg":
                    return data
    return None


def load_inline_bytes(path: Path) -> tuple[bytes, str] | None:
    """Bytes and media type to embed, or None when the file is not a usable image."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    media = sniff_media_type(data)
    if media is None:
        return None
    if len(data) <= INLINE_TARGET_BYTES:
        return data, media
    shrunk = shrink_image_bytes(path, INLINE_TARGET_BYTES)
    if shrunk is None:
        return None
    return shrunk, "image/jpeg"


def claude_stream_message(prompt: str, images: Sequence[tuple[bytes, str]]) -> str:
    """One Claude `--input-format stream-json` user turn. Images come first."""
    content: list[dict[str, object]] = []
    for data, media in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": prompt})
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": content}},
        separators=(",", ":"),
    ) + "\n"


def grok_prompt_json(prompt: str, images: Sequence[tuple[bytes, str]]) -> str:
    """ACP content blocks for Grok `--prompt-json`. Images come first."""
    blocks: list[dict[str, str]] = []
    for data, media in images:
        blocks.append(
            {
                "type": "image",
                "mimeType": media,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    blocks.append({"type": "text", "text": prompt})
    return json.dumps(blocks, separators=(",", ":"))


def inlined_images(
    prompt: str,
    paths: Sequence[Path],
    *,
    kind: str,
) -> list[tuple[bytes, str]]:
    """Images that fit the provider's prompt budget, in path order.

    An empty list means the caller should send the text prompt alone. The text
    still names the local files.
    """
    loaded: list[tuple[bytes, str]] = []
    for path in paths:
        item = load_inline_bytes(path)
        if item is not None:
            loaded.append(item)
    if not loaded:
        return []
    budget = GROK_PROMPT_JSON_BUDGET if kind == "grok" else CLAUDE_STDIN_BUDGET
    builder = grok_prompt_json if kind == "grok" else claude_stream_message
    while loaded and len(builder(prompt, loaded).encode("utf-8")) > budget:
        loaded.pop()
    return loaded


def codex_image_flags(paths: Sequence[Path]) -> list[str]:
    """Repeated `--image` flags. One path per flag so a following option is not consumed."""
    flags: list[str] = []
    for path in paths:
        flags.extend(["--image", str(path)])
    return flags


def assistant_result_text(raw: str) -> str:
    """Final text from Claude `json` or `stream-json` stdout. Empty when it is not that shape."""
    found = ""
    saw_result = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("result"), str):
            found = event["result"]
            saw_result = True
    if saw_result:
        return found
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict) and payload.get("type") == "result" and isinstance(payload.get("result"), str):
        return str(payload["result"])
    return ""
