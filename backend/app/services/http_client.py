from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.settings import get_settings
from app.db.database import get_connection, init_db


class ExternalApiError(RuntimeError):
    def __init__(self, service: str, message: str, status_code: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status_code = status_code


class AsyncRateLimiter:
    def __init__(self, rate_limit_per_second: float) -> None:
        self.interval = 0.0 if rate_limit_per_second <= 0 else 1.0 / rate_limit_per_second
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_for = self.interval - (now - self._last_call)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_call = time.monotonic()


class ApiCache:
    def __init__(self, service: str) -> None:
        self.service = service
        init_db()

    def get(self, cache_key: str) -> Any | None:
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT response_body, expires_at
                FROM api_cache
                WHERE service = ? AND cache_key = ?
                """,
                (self.service, cache_key),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] <= now:
                conn.execute(
                    "DELETE FROM api_cache WHERE service = ? AND cache_key = ?",
                    (self.service, cache_key),
                )
                return None
            return json.loads(row["response_body"])

    def set(self, cache_key: str, status_code: int, payload: Any, ttl_seconds: int) -> None:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO api_cache(service, cache_key, status_code, response_body, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(service, cache_key) DO UPDATE SET
                    status_code = excluded.status_code,
                    response_body = excluded.response_body,
                    expires_at = excluded.expires_at,
                    created_at = CURRENT_TIMESTAMP
                """,
                (self.service, cache_key, status_code, json.dumps(payload), expires_at),
            )


class CachingHttpClient:
    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        service: str,
        base_url: str,
        rate_limit_per_second: float,
        headers: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self.service = service
        self.max_retries = settings.api_max_retries
        self.cache_ttl_seconds = settings.api_cache_ttl_seconds
        self.rate_limiter = AsyncRateLimiter(rate_limit_per_second)
        self.cache = ApiCache(service)
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, **(headers or {})},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_ttl_seconds: int | None = None,
    ) -> Any:
        cache_key = self._cache_key("GET", path, params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                response = await self.client.get(path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise ExternalApiError(self.service, str(exc)) from exc
                await asyncio.sleep(self._retry_delay(attempt))
                continue

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise ExternalApiError(self.service, "Invalid JSON response", response.status_code) from exc
                self.cache.set(
                    cache_key,
                    response.status_code,
                    payload,
                    cache_ttl_seconds or self.cache_ttl_seconds,
                )
                return payload

            if response.status_code not in self.RETRYABLE_STATUS_CODES:
                raise ExternalApiError(
                    self.service,
                    self._error_message(response),
                    response.status_code,
                )

            last_error = ExternalApiError(self.service, self._error_message(response), response.status_code)
            if attempt >= self.max_retries:
                raise last_error
            await asyncio.sleep(self._retry_delay(attempt, response))

        raise ExternalApiError(self.service, str(last_error or "Request failed"))

    def _cache_key(self, method: str, path: str, params: dict[str, Any] | None) -> str:
        payload = json.dumps(
            {"method": method, "path": path, "params": params or {}},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 60.0)
        base = min(2**attempt, 30)
        return base + random.uniform(0, 0.25)

    def _error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("message") or payload.get("error") or payload)
        except (json.JSONDecodeError, sqlite3.Error):
            pass
        return response.text[:500] or f"HTTP {response.status_code}"

