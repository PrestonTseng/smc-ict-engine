"""Bounded JSON transport used only by public market-data adapters."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smc_ict.application.ports import (
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTemporaryError,
)


class JsonRequester:
    """GET JSON with bounded retries and provider-neutral error translation."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        backoff_seconds: Sequence[float] = (1.0, 2.0),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._backoffs = tuple(backoff_seconds)
        self._sleep = sleeper

    def __call__(self, path: str, parameters: dict[str, object]) -> object:
        url = f"{self._base_url}{path}"
        if parameters:
            url = f"{url}?{urlencode(parameters)}"
        for attempt in range(len(self._backoffs) + 1):
            try:
                request = Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "smc-ict-engine/0.1 public-market-data",
                    },
                    method="GET",
                )
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    return self._decode(response.read())
            except HTTPError as exc:
                retry_after_ms = self._retry_after_ms(exc.headers)
                if exc.code in {418, 429}:
                    if attempt < len(self._backoffs):
                        self._sleep(
                            retry_after_ms / 1000
                            if retry_after_ms is not None
                            else self._backoffs[attempt]
                        )
                        continue
                    raise ProviderRateLimitError(retry_after_ms) from None
                if 500 <= exc.code < 600:
                    if attempt < len(self._backoffs):
                        self._sleep(self._backoffs[attempt])
                        continue
                    raise ProviderTemporaryError("provider service remained unavailable") from None
                raise ProviderPermanentError(
                    f"provider rejected request with HTTP {exc.code}"
                ) from None
            except (TimeoutError, URLError):
                if attempt < len(self._backoffs):
                    self._sleep(self._backoffs[attempt])
                    continue
                raise ProviderTemporaryError("provider transport remained unavailable") from None
        raise AssertionError("bounded retry loop exhausted unexpectedly")

    @staticmethod
    def _decode(body: bytes) -> object:
        try:
            return json.loads(body)
        except (UnicodeDecodeError, JSONDecodeError) as exc:
            raise ProviderProtocolError("provider returned malformed JSON") from exc

    @staticmethod
    def _retry_after_ms(headers: Message | Mapping[str, str] | None) -> int | None:
        if headers is None:
            return None
        value = headers.get("Retry-After")
        if value is None or not value.isdigit():
            return None
        return int(value) * 1000
