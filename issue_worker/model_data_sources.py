"""Bounded public HTTPS adapters for model-data calibration.

Source schemas: https://github.com/anomalyco/models.dev and
https://artificialanalysis.ai/api-reference. Secrets are read only from the
process environment, never accepted in URLs or returned in error messages.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

MODELS_DEV_URL = "https://models.dev/api.json"
ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
MAX_SOURCE_BYTES = 12_000_000
ARTIFICIAL_ANALYSIS_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
ALLOWED_KINDS = ("models_dev", "artificial_analysis", "json")


class SourceError(ValueError):
    pass


def _require_public_https(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise SourceError("Use a public HTTPS source URL without credentials, query, or fragment.")
    return parsed.hostname, parsed.path or "/"


def fetch_json(url: str, *, api_key: str | None = None) -> tuple[object, str]:
    """Pin the resolved public address, verify TLS hostname, refuse redirects.

    Configurable feeds cannot access loopback/private metadata services or
    forward an API key to a redirect target. Total read time/size is bounded.
    """
    try:
        hostname, path = _require_public_https(url)
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise SourceError("Model sources must resolve only to public internet addresses.")
        address = addresses[0][4][0]
        conn = http.client.HTTPSConnection(hostname, timeout=10)
        # Connecting to the checked IP prevents a second DNS lookup/rebinding.
        raw_socket = socket.create_connection((address, 443), timeout=10)
        try:
            conn.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=hostname)
        except Exception:
            raw_socket.close()
            raise
        try:
            headers = {"Accept": "application/json", "User-Agent": "SWARM-Model-Calibration/1"}
            if api_key:
                if url != ARTIFICIAL_ANALYSIS_URL:
                    raise SourceError("Benchmark credentials may only be sent to Artificial Analysis.")
                headers["x-api-key"] = api_key
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise SourceError(
                    f"Model source returned HTTP {response.status}; existing calibration remains active."
                )
            chunks, size, deadline = [], 0, time.monotonic() + 20
            while True:
                chunk = response.read(65536)
                size += len(chunk)
                if size > MAX_SOURCE_BYTES or time.monotonic() > deadline:
                    raise SourceError("Model source exceeded the response size or time limit.")
                if not chunk:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            conn.close()
        payload = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return payload, hashlib.sha256(body).hexdigest()
    except SourceError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        # Never interpolate URL, response body, headers, or exception text.
        raise SourceError(
            "Could not read model source: check connectivity, TLS, credentials, and JSON format."
        ) from None


def fetch_source(kind: str, url: str = "") -> tuple[list[dict], dict]:
    if kind not in ALLOWED_KINDS:
        raise SourceError("Unknown model data source.")
    target = {"models_dev": MODELS_DEV_URL, "artificial_analysis": ARTIFICIAL_ANALYSIS_URL}.get(kind, url)
    api_key = os.environ.get(ARTIFICIAL_ANALYSIS_KEY_ENV) if kind == "artificial_analysis" else None
    if kind == "artificial_analysis" and not api_key:
        raise SourceError("No Artificial Analysis API key is configured.")
    if kind == "json" and not target:
        raise SourceError("A JSON model source needs an HTTPS URL.")
    payload, digest = fetch_json(target, api_key=api_key)
    rows: list[dict] = []
    try:
        if kind == "models_dev":
            if not isinstance(payload, dict):
                raise SourceError("Model source response does not match its documented schema.")
            for provider in ("anthropic", "openai", "xai"):
                models = (payload.get(provider) or {}).get("models") or {}
                if not isinstance(models, dict):
                    continue
                for model_id, model in models.items():
                    if not isinstance(model, dict):
                        continue
                    price = model.get("cost") or {}
                    if not isinstance(price, dict):
                        price = {}
                    rows.append(
                        {
                            "provider": provider,
                            "model": str(model_id),
                            "input_cost": price.get("input"),
                            "output_cost": price.get("output"),
                            "reasoning_cost": price.get("reasoning"),
                            "release_date": model.get("release_date"),
                            "deprecated": model.get("status") == "deprecated",
                        }
                    )
        elif kind == "artificial_analysis":
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise SourceError("Model source response does not match its documented schema.")
            for model in payload["data"]:
                if not isinstance(model, dict):
                    continue
                creator = model.get("model_creator") or {}
                price = model.get("pricing") or {}
                if not isinstance(creator, dict) or not isinstance(price, dict):
                    continue
                rows.append(
                    {
                        "provider": creator.get("slug"),
                        "model": model.get("slug"),
                        "source_id": model.get("id"),
                        "input_cost": price.get("price_1m_input_tokens"),
                        "output_cost": price.get("price_1m_output_tokens"),
                        "evaluations": model.get("evaluations") if isinstance(model.get("evaluations"), dict) else {},
                        "speed": model.get("median_output_tokens_per_second"),
                        "latency_seconds": model.get("median_time_to_first_token_seconds"),
                    }
                )
        else:
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                rows = [row for row in payload["models"] if isinstance(row, dict)]
            elif isinstance(payload, list):
                rows = [row for row in payload if isinstance(row, dict)]
            else:
                raise SourceError("Model source response does not match its documented schema.")
    except SourceError:
        raise
    except (KeyError, TypeError, AttributeError):
        raise SourceError("Model source response does not match its documented schema.") from None
    return rows, {"kind": kind, "version": digest, "status": "ok", "url": None if kind != "json" else target}
