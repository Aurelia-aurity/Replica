"""Supabase Storage 클라이언트: 기록 원본 파일의 서명 업로드 주소 발급, 존재·크기 확인, 읽기.

비공개 버킷 `replica-records`만 쓴다. 서버 키(secret)로만 호출하며, 앱에는 서명 업로드 주소만 준다.
토큰·서명 주소·파일 내용은 로그와 예외 문자열에 넣지 않는다.

주의: 버킷은 만들어져 있지만 실제 서명 업로드는 아직 해 보지 않았다. 서명 주소 발급(`/object/upload/sign`)과
정보 조회(`/object/info`)의 응답 형식은 Supabase 문서 기준 가정이며, 실제 호출로 확인해야 한다(#120).
"""

import asyncio
import base64
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app import db

BUCKET = "replica-records"
# 서명 업로드 토큰의 유효 시간을 읽지 못할 때 쓰는 기본값(Supabase 서명 업로드 주소는 약 2시간).
DEFAULT_UPLOAD_TTL = timedelta(hours=2)


class StorageUnavailable(Exception):
    """Storage에 연결하지 못했거나 예상하지 못한 응답을 받았다. 원인 상세는 담지 않는다."""


@dataclass(frozen=True)
class SignedUpload:
    url: str
    headers: Mapping[str, str]
    expires_at: datetime


class StorageClient(Protocol):
    async def create_upload(self, object_path: str, *, mime_type: str) -> SignedUpload: ...

    async def object_size(self, object_path: str) -> int | None:
        """원본의 크기(바이트). 객체가 없으면 None."""

    async def read_object(self, object_path: str, *, max_bytes: int) -> bytes: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # apikey·Authorization을 다른 주소로 넘기지 않는다.


def _token_expiry(token: str, now: datetime) -> datetime:
    """서명 토큰(JWT)의 만료 시각. 읽지 못하면 기본 유효 시간을 쓴다. 서명은 검증하지 않는다(값 표시용)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (IndexError, KeyError, ValueError, TypeError, OSError, OverflowError):
        return now + DEFAULT_UPLOAD_TTL


class SupabaseStorage:
    """블로킹 I/O는 asyncio 기본 실행기에서 돌린다. 자동 재시도는 없다."""

    def __init__(self, settings: db.Settings):
        self._settings = settings
        key = settings.secret_key
        self._headers = {"apikey": key}
        if not key.startswith("sb_secret_"):
            self._headers["Authorization"] = "Bearer " + key  # 예전 service_role 키만 JWT다.

    def _base(self) -> str:
        return self._settings.url.rstrip("/")

    @staticmethod
    def _path(object_path: str) -> str:
        return "/".join(quote(part, safe="") for part in object_path.split("/"))

    def _call(self, method: str, path: str, *, max_bytes: int, body: bytes | None = None,
              extra: Mapping[str, str] | None = None) -> tuple[int, bytes]:
        request = Request(self._base() + path, data=body, method=method,
                          headers={**self._headers, **(extra or {})})
        try:
            try:
                response = build_opener(_NoRedirect()).open(request, timeout=self._settings.timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise StorageUnavailable()
                return response.code, raw
        except (URLError, OSError, socket.timeout):
            raise StorageUnavailable() from None

    async def create_upload(self, object_path: str, *, mime_type: str) -> SignedUpload:
        path = f"/storage/v1/object/upload/sign/{BUCKET}/{self._path(object_path)}"
        status, raw = await asyncio.to_thread(
            self._call, "POST", path, max_bytes=65536, body=b"{}",
            extra={"Content-Type": "application/json"})
        try:
            data = json.loads(raw)
            signed, token = data["url"], data["token"]
            if status != 200 or not isinstance(signed, str) or not isinstance(token, str):
                raise ValueError
            # 응답의 url은 `/object/upload/sign/...?token=...` 형태의 상대 경로여야 한다. 다른 주소는 받지 않는다.
            if not signed.startswith("/") or signed.startswith("//"):
                raise ValueError
            url = self._base() + "/storage/v1" + signed
        except (ValueError, KeyError, TypeError):
            raise StorageUnavailable() from None
        return SignedUpload(url=url, headers={"Content-Type": mime_type},
                            expires_at=_token_expiry(token, datetime.now(timezone.utc)))

    async def object_size(self, object_path: str) -> int | None:
        path = f"/storage/v1/object/info/{BUCKET}/{self._path(object_path)}"
        status, raw = await asyncio.to_thread(self._call, "GET", path, max_bytes=65536)
        if status in (400, 404):
            return None  # Storage는 없는 객체를 404(또는 400 not_found)로 알린다.
        try:
            size = json.loads(raw)["size"]
        except (ValueError, KeyError, TypeError):
            raise StorageUnavailable() from None
        if status != 200 or type(size) is not int or size < 0:
            raise StorageUnavailable()
        return size

    async def read_object(self, object_path: str, *, max_bytes: int) -> bytes:
        path = f"/storage/v1/object/{BUCKET}/{self._path(object_path)}"
        status, raw = await asyncio.to_thread(self._call, "GET", path, max_bytes=max_bytes)
        if status != 200:
            raise StorageUnavailable()
        return raw
