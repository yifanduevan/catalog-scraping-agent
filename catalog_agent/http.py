from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from typing import Any

LOGGER = logging.getLogger(__name__)


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        min_interval_seconds: float = 1,
        max_retries: int = 3,
        user_agent: str = "CatalogScrapingAgent/0.1 (+take-home prototype)",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._last_request_at = 0.0

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
            },
        )
        return self._request(request).decode("utf-8", errors="replace")

    def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json",
                **headers,
            },
        )
        return json.loads(self._request(request))

    def _request(self, request: urllib.request.Request) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc

            if attempt < self.max_retries:
                delay = (2**attempt) + random.uniform(0, 0.25)
                LOGGER.warning(
                    "Request failed; retrying",
                    extra={
                        "url": request.full_url,
                        "attempt": attempt + 1,
                        "delay_seconds": round(delay, 2),
                    },
                )
                time.sleep(delay)

        assert last_error is not None
        raise last_error

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

