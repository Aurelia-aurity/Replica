"""Small async REST adapter with no third-party runtime dependencies."""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import DatabaseError, DatabaseUnavailableError, ValidationError


@dataclass(frozen=True)
class Settings:
    url: str
    publishable_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    timeout_seconds: float = 10.0
    allow_local_http: bool = False

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.url)
            parts.port
        except (ValueError, TypeError, AttributeError):
            raise ValidationError() from None
        local = self.allow_local_http and parts.hostname in ("localhost", "127.0.0.1", "::1")
        if (parts.scheme != "https" and not (local and parts.scheme == "http")) or not parts.hostname:
            raise ValidationError()
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValidationError()
        if not all(isinstance(k, str) and k and k.isascii() and not any(c.isspace() for c in k)
                   for k in (self.publishable_key, self.secret_key)):
            raise ValidationError()
        if self.publishable_key.startswith("sb_secret_") or self.secret_key.startswith("sb_publishable_"):
            raise ValidationError()
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (float, int))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120):
            raise ValidationError()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        try:
            return cls(
                url=values["SUPABASE_URL"],
                publishable_key=values.get("SUPABASE_PUBLISHABLE_KEY") or values["SUPABASE_ANON_KEY"],
                secret_key=values.get("SUPABASE_SECRET_KEY") or values["SUPABASE_SERVICE_ROLE_KEY"],
                timeout_seconds=float(values.get("SUPABASE_DB_TIMEOUT_SECONDS", "10")),
            )
        except (KeyError, ValueError, TypeError):
            raise ValidationError() from None


@dataclass(frozen=True)
class Response:
    status: int
    data: Any


class Transport(Protocol):
    async def request(self, method: str, path: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str], body: Any = None) -> Response: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization or apikey to a redirect target.


class UrllibTransport:
    """Blocking I/O runs in asyncio's bounded default executor; no implicit retry."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def request(self, method: str, path: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str], body: Any = None) -> Response:
        return await asyncio.to_thread(self._request, method, path, dict(headers), dict(params), body)

    def _request(self, method: str, path: str, headers: dict[str, str],
                 params: dict[str, str], body: Any) -> Response:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValidationError()
        url = self._settings.url.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        try:
            data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ValidationError() from None
        headers = {**headers, "Accept": "application/json", "Content-Type": "application/json"}
        request = Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = build_opener(_NoRedirect()).open(request, timeout=self._settings.timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                # Bound memory; raw error responses are intentionally discarded later.
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise DatabaseError(code="response_too_large", outcome_unknown=method != "GET")
                try:
                    payload = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    if response.code >= 400:
                        payload = None
                    else:
                        raise DatabaseError(code="invalid_response", outcome_unknown=method != "GET") from None
                return Response(response.code, payload)
        except (URLError, OSError, socket.timeout):
            raise DatabaseUnavailableError(outcome_unknown=method != "GET") from None
