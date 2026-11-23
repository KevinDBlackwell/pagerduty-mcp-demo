"""PagerDuty REST client shared by all the tools.

Follows offset/limit pagination up to a per-call cap, retries 429s, 5xxs and timeouts
a few times, and turns HTTP failures into PagerDutyError with a hint for the model.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

API_BASE = "https://api.pagerduty.com"
PAGE_LIMIT = 100          # PagerDuty's maximum page size for classic pagination
DEFAULT_MAX_ITEMS = 500   # safety cap per paginated call
MAX_RETRIES = 3


@dataclass
class PagerDutyError(Exception):
    """API failure with a message and a hint the model can act on."""

    status: int | None
    message: str
    hint: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.message} {self.hint}".strip()


def _hint_for(status: int) -> tuple[str, str, bool]:
    """Map an HTTP status to (message, hint, retryable)."""
    if status == 401:
        return ("PagerDuty rejected the API token.",
                "Tell the user the token is missing or invalid. Do not retry.", False)
    if status == 403:
        return ("The API token is not allowed to read this resource.",
                "Tell the user which data you could not access. Do not retry.", False)
    if status == 404:
        return ("PagerDuty could not find that resource.",
                "Check the ID. For services, call find_services to get a valid ID.", False)
    if status == 429:
        return ("PagerDuty rate limit reached and retries ran out.",
                "Wait about a minute before calling PagerDuty tools again.", True)
    if status >= 500:
        return ("PagerDuty returned a server error.",
                "Retry once later. If it keeps failing, tell the user PagerDuty may be degraded.", True)
    return (f"PagerDuty returned HTTP {status}.", "Check the arguments and try again.", False)


class PagerDutyClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        base_url: str = API_BASE,
    ):
        token = token or os.environ.get("PAGERDUTY_API_TOKEN")
        if not token:
            raise PagerDutyError(
                None,
                "No PagerDuty API token is configured (PAGERDUTY_API_TOKEN is unset or empty).",
                "Tell the user to set PAGERDUTY_API_TOKEN in the MCP server's environment and restart it. Do not retry.",
            )
        self._sleep = sleep  # tests pass a no-op
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Token token={token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
                "User-Agent": "pagerduty-oncall-mcp/0.1",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_status = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.get(path, params=params)
            except httpx.TimeoutException:
                last_status = None
                if attempt < MAX_RETRIES:
                    self._sleep(self._backoff(attempt))
                    continue
                raise PagerDutyError(None, "PagerDuty did not respond in time.",
                                     "Retry once. If it fails again, tell the user.", True)
            except httpx.HTTPError as exc:
                raise PagerDutyError(None, f"Could not reach PagerDuty ({exc.__class__.__name__}).",
                                     "Tell the user there is a network problem.", True)

            if resp.status_code < 400:
                return resp.json()

            last_status = resp.status_code
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < MAX_RETRIES:
                self._sleep(self._retry_delay(resp, attempt))
                continue
            break

        message, hint, retryable = _hint_for(last_status or 0)
        raise PagerDutyError(last_status, message, hint, retryable)

    @staticmethod
    def _backoff(attempt: int) -> float:
        # 1, 2, 4, 8s plus jitter
        return min(2 ** attempt, 8) + random.uniform(0, 0.5)

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        # PagerDuty sends ratelimit-reset on 429s. Retry-After is checked too in case a proxy sets it.
        for header in ("ratelimit-reset", "retry-after"):
            value = resp.headers.get(header)
            if value:
                try:
                    return min(float(value), 30.0)
                except ValueError:
                    pass
        return self._backoff(attempt)

    def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Collect items across pages. Returns (items, truncated)."""
        items: list[dict[str, Any]] = []
        for page_items, more in self._pages(path, key, params):
            items.extend(page_items)
            if len(items) >= max_items:
                return items[:max_items], True
            if not more:
                break
        return items, False

    def _pages(self, path, key, params) -> Iterator[tuple[list[dict], bool]]:
        offset = 0
        base = dict(params or {})
        while True:
            data = self.get(path, {**base, "limit": PAGE_LIMIT, "offset": offset})
            page = data.get(key, [])
            more = bool(data.get("more"))
            yield page, more
            if not more or not page:
                return
            offset += len(page)

    def close(self) -> None:
        self._http.close()
