"""Object storage behind the hosted ``Storage``: checkpoints, logs and documents.

``ObjectStore`` is the small surface the hosted storage needs from an
S3-compatible service: whole-object get/put/delete, a prefix listing and
*conditional* writes (``If-Match`` / ``If-None-Match``), which is what makes a
log append safe without a lock. Two implementations:

* :class:`MemoryObjectStore` - in-process, for tests and single-process use;
* :class:`S3ObjectStore` - AWS S3, MinIO and other S3-compatible services over
  plain HTTP(S) with Signature Version 4, written against the standard library
  so the worker image carries no SDK.

Nothing here knows about tenants or the storage layout; ``storage_remote.py``
builds keys. Credentials live only in the instance, are never part of an error
message or ``repr``, and a plain-HTTP endpoint is refused unless it is loopback
(local MinIO) or explicitly allowed, so keys are not signed onto cleartext links
by accident.
"""

from __future__ import annotations

import abc
import datetime as dt
import hashlib
import hmac
import ipaddress
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from typing import Mapping, NamedTuple


class ObjectStoreError(Exception):
    """The object store failed (network, permissions, malformed reply)."""


class PreconditionFailed(ObjectStoreError):
    """A conditional write lost the race: the object changed (or exists)."""


class ObjectNotFound(ObjectStoreError):
    """The object (or bucket) does not exist."""


class ObjectData(NamedTuple):
    data: bytes
    etag: str


class ObjectStore(abc.ABC):
    @abc.abstractmethod
    def get(self, key: str) -> ObjectData | None:
        """The object, or ``None`` when absent."""

    @abc.abstractmethod
    def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream",
        if_match: str | None = None, if_none_match: bool = False,
    ) -> None:
        """Atomically replace the object. ``if_match`` requires the current ETag;
        ``if_none_match`` requires the object to be absent. Either failing
        raises :class:`PreconditionFailed` and writes nothing."""

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Remove the object; ``True`` when it existed."""

    @abc.abstractmethod
    def list(self, prefix: str) -> list[str]:
        """Every key beginning with ``prefix``, sorted."""


def _etag(data: bytes) -> str:
    return '"%s"' % hashlib.md5(data, usedforsecurity=False).hexdigest()


class MemoryObjectStore(ObjectStore):
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> ObjectData | None:
        with self._lock:
            data = self._objects.get(key)
        return None if data is None else ObjectData(data, _etag(data))

    def put(self, key, data, *, content_type="application/octet-stream", if_match=None, if_none_match=False):
        if not isinstance(data, (bytes, bytearray)):
            raise ObjectStoreError("Object data must be bytes")
        with self._lock:
            current = self._objects.get(key)
            if if_none_match and current is not None:
                raise PreconditionFailed(key)
            if if_match is not None and (current is None or _etag(current) != if_match):
                raise PreconditionFailed(key)
            self._objects[key] = bytes(data)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._objects.pop(key, None) is not None

    def list(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(key for key in self._objects if key.startswith(prefix))


# -- Signature Version 4 ----------------------------------------------------

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def canonical_query(query: Mapping[str, str]) -> str:
    return "&".join(
        f"{urllib.parse.quote(str(name), safe='-_.~')}={urllib.parse.quote(str(value), safe='-_.~')}"
        for name, value in sorted(query.items())
    )


def sigv4_signature(
    *, method: str, canonical_uri: str, query: Mapping[str, str], headers: Mapping[str, str],
    payload_sha256: str, amz_date: str, region: str, service: str, secret_key: str,
) -> tuple[str, str]:
    """``(signature, signed_headers)`` for one request.

    ``headers`` are the headers to sign (any case); ``amz_date`` is
    ``YYYYMMDDTHHMMSSZ``. Exposed so the AWS-published test vector can pin it.
    """
    lowered = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed_headers = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in sorted(lowered))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query(query), canonical_headers, signed_headers, payload_sha256]
    )
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest(), signed_headers


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class S3ObjectStore(ObjectStore):
    """S3-compatible store addressed as ``<endpoint>/<bucket>/<key>`` (path
    style, what MinIO wants) or ``<bucket>.<endpoint host>/<key>``."""

    def __init__(
        self, *, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1",
        session_token: str = "", path_style: bool = True, timeout: float = 30.0,
        allow_insecure_http: bool = False,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ObjectStoreError("S3 endpoint must be an http(s) URL without credentials")
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ObjectStoreError("S3 endpoint must be a bare scheme://host[:port]")
        if parsed.scheme == "http" and not (allow_insecure_http or _is_loopback(parsed.hostname)):
            raise ObjectStoreError("Refusing to send S3 credentials over plain HTTP to a non-loopback host")
        if not bucket or not access_key or not secret_key:
            raise ObjectStoreError("S3 bucket and credentials are required")
        self._scheme = parsed.scheme
        self._netloc = parsed.netloc
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._session_token = session_token
        self._region = region
        self._path_style = path_style
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:  # never print credentials
        return f"S3ObjectStore(endpoint={self._scheme}://{self._netloc}, bucket={self._bucket!r})"

    # -- request plumbing --------------------------------------------------
    def _request(
        self, method: str, key: str = "", *, query: Mapping[str, str] | None = None, body: bytes = b"",
        headers: Mapping[str, str] | None = None, ok: tuple[int, ...] = (200,),
    ) -> tuple[int, Mapping[str, str], bytes]:
        query = dict(query or {})
        if self._path_style:
            host = self._netloc
            path = "/" + urllib.parse.quote(self._bucket, safe="") + ("/" + key if key else "")
        else:
            host = f"{self._bucket}.{self._netloc}"
            path = "/" + key
        canonical_uri = urllib.parse.quote(path, safe="/-_.~")
        amz_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
        signed = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
        if self._session_token:
            signed["x-amz-security-token"] = self._session_token
        extra = {name.lower(): value for name, value in (headers or {}).items()}
        for name in ("if-match", "if-none-match", "content-type"):
            if name in extra:
                signed[name] = extra[name]
        signature, signed_headers = sigv4_signature(
            method=method, canonical_uri=canonical_uri, query=query, headers=signed, payload_sha256=payload,
            amz_date=amz_date, region=self._region, service="s3", secret_key=self._secret_key,
        )
        scope = f"{amz_date[:8]}/{self._region}/s3/aws4_request"
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self._scheme}://{host}{canonical_uri}" + (f"?{canonical_query(query)}" if query else "")
        request = urllib.request.Request(url, data=body if body or method == "PUT" else None, method=method)
        for name, value in signed.items():
            if name != "host":
                request.add_header(name, value)
        request.add_header("Authorization", authorization)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status, reply_headers, reply = response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            status, reply_headers, reply = error.code, dict(error.headers or {}), error.read()
            error.close()
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ObjectStoreError(f"S3 {method} failed: {getattr(error, 'reason', error)}") from error
        if status in ok:
            return status, reply_headers, reply
        if status == 404:
            raise ObjectNotFound(f"S3 {method} returned 404 ({_error_code(reply)})")
        if status in (409, 412) and method == "PUT":
            raise PreconditionFailed(f"S3 precondition failed ({status})")
        raise ObjectStoreError(f"S3 {method} returned {status} ({_error_code(reply)})")

    # -- ObjectStore -------------------------------------------------------
    def get(self, key: str) -> ObjectData | None:
        try:
            _, headers, data = self._request("GET", key)
        except ObjectNotFound:
            return None
        return ObjectData(data, _header(headers, "etag"))

    def put(self, key, data, *, content_type="application/octet-stream", if_match=None, if_none_match=False):
        headers = {"content-type": content_type}
        if if_match is not None:
            headers["if-match"] = if_match
        if if_none_match:
            headers["if-none-match"] = "*"
        self._request("PUT", key, body=bytes(data), headers=headers, ok=(200, 201, 204))

    def delete(self, key: str) -> bool:
        try:
            self._request("HEAD", key)
        except ObjectNotFound:
            return False
        self._request("DELETE", key, ok=(200, 204))
        return True

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            _, _, body = self._request("GET", "", query=query)
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError as error:
                raise ObjectStoreError("S3 returned a malformed listing") from error
            keys.extend(
                element.text or ""
                for element in root.iter()
                if _local(element.tag) == "Key" and element.text is not None
            )
            truncated = next((e.text for e in root.iter() if _local(e.tag) == "IsTruncated"), "false")
            token = next((e.text or "" for e in root.iter() if _local(e.tag) == "NextContinuationToken"), "")
            if truncated != "true" or not token:
                return sorted(keys)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would replay a request signed for another host."""

    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def _error_code(body: bytes) -> str:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return "no error code"
    return next((e.text or "" for e in root.iter() if _local(e.tag) == "Code"), "no error code")
