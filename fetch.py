"""HTTP helpers. Cloudflare Blog rejects a bare client user agent."""

from __future__ import annotations

import threading
import time

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
RETRY_STATUS = {429, 500, 502, 503, 504}
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_UNTIL = 0.0


def timeout() -> httpx.Timeout:
    return httpx.Timeout(90.0, connect=20.0)


def client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout())


def async_client(*, connections: int = 8) -> httpx.AsyncClient:
    connections = max(1, connections)
    return httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=timeout(),
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
    )


def _error_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def _wait_for_cooldown() -> float:
    with _COOLDOWN_LOCK:
        return max(0.0, _COOLDOWN_UNTIL - time.monotonic())


def _note_cooldown(seconds: float) -> None:
    global _COOLDOWN_UNTIL
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + seconds)


def _pause(attempt: int, response: httpx.Response | None) -> float:
    """Back off longer after 429, and hold every later request until it passes."""
    if response is not None and response.status_code == 429:
        header = response.headers.get("retry-after", "").strip()
        delay = min(int(header), 30) if header.isdigit() else min(2 ** (attempt + 1), 20)
        _note_cooldown(delay)
        return delay
    return 1.2 * (attempt + 1)


def get(http: httpx.Client, url: str) -> httpx.Response:
    last_error = "request failed"
    for attempt in range(6):
        remaining = _wait_for_cooldown()
        if remaining:
            time.sleep(remaining)
        response = None
        try:
            response = http.get(url)
        except httpx.HTTPError as exc:
            last_error = _error_text(exc)
        else:
            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
            else:
                response.raise_for_status()
                return response
        if response is None or response.status_code != 429:
            if attempt >= 3:
                break
        time.sleep(_pause(attempt, response))
    raise RuntimeError(f"{url}: {last_error}")


async def get_text(http: httpx.AsyncClient, url: str) -> str:
    last_error = "request failed"
    for attempt in range(6):
        remaining = _wait_for_cooldown()
        if remaining:
            await _async_sleep(remaining)
        response = None
        try:
            response = await http.get(url)
        except httpx.HTTPError as exc:
            last_error = _error_text(exc)
        else:
            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
            else:
                response.raise_for_status()
                return response.text
        if response is None or response.status_code != 429:
            if attempt >= 3:
                break
        await _async_sleep(_pause(attempt, response))
    raise RuntimeError(f"{url}: {last_error}")


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
