"""Generic HTTPS JSON webhook adapter with boundary-only secret resolution."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from time import sleep, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from smc_ict.application.ports.notifications import DeliveryReceipt, NotificationEvent
from smc_ict.configuration.models import NotificationDestination, SecretRef


class GenericWebhookNotifier:
    adapter_id = "generic_webhook"

    def __init__(
        self,
        destination_id: str,
        destination: NotificationDestination,
        *,
        environ: Mapping[str, str] | None = None,
        opener: Callable[..., object] = urlopen,
        sleeper: Callable[[int], None] = sleep,
        clock_seconds: Callable[[], int] = lambda: int(time()),
    ) -> None:
        self._destination_id = destination_id
        self._destination = destination
        self._endpoint = self._resolve(destination.endpoint, environ)
        self._opener = opener
        self._sleeper = sleeper
        self._clock_seconds = clock_seconds

    def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
        return self._deliver_body((event,), batched=False)

    def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        if not 1 <= len(events) <= self._destination.batching.maximum_events:
            raise ValueError("notification batch size is outside configured bounds")
        return self._deliver_body(events, batched=True)

    def _deliver_body(
        self, events: tuple[NotificationEvent, ...], *, batched: bool
    ) -> DeliveryReceipt:
        event_values = [self._event_dict(event) for event in events]
        event_id = self._hash(
            [(event.event_type, event.run_id, event.instrument_id) for event in events]
        )
        deduplication_id = self._hash(
            {"destination_id": self._destination_id, "event_id": event_id}
        )
        batch_id = self._hash(
            {
                "destination_id": self._destination_id,
                "window": self._clock_seconds() // self._destination.batching.flush_seconds,
            }
        )
        body = json.dumps(
            {"events": event_values} if batched else event_values[0],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        for attempt in range(1, self._destination.retries.maximum_attempts + 1):
            try:
                request = Request(
                    self._endpoint,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self._opener(  # type: ignore[attr-defined]
                    request, timeout=float(self._destination.timeout_seconds)
                ) as response:
                    status = response.getcode()
                if 200 <= status < 300:
                    return DeliveryReceipt(
                        self._destination_id,
                        self.adapter_id,
                        event_id,
                        deduplication_id,
                        batch_id,
                        attempt,
                        "SUCCESS",
                        None,
                        status,
                    )
                reason = f"HTTP_{status}"
            except HTTPError as exc:
                status = exc.code
                reason = f"HTTP_{status}"
            except (OSError, URLError, TimeoutError):
                status = None
                reason = "TRANSPORT_ERROR"
            retryable = status is None or status in {408, 429} or status >= 500
            if not retryable:
                break
            if attempt < self._destination.retries.maximum_attempts:
                self._sleeper(self._destination.retries.backoff_seconds[attempt - 1])
        return DeliveryReceipt(
            self._destination_id,
            self.adapter_id,
            event_id,
            deduplication_id,
            batch_id,
            attempt,
            "FAILURE",
            reason,
            status,
        )

    @staticmethod
    def _event_dict(event: NotificationEvent) -> dict[str, object]:
        return {
            "event_type": event.event_type,
            "run_id": event.run_id,
            "instrument_id": event.instrument_id,
            "strategy_id": event.strategy_id,
            "payload_schema_version": event.payload_schema_version,
            "payload": dict(event.payload),
        }

    @staticmethod
    def _resolve(ref: SecretRef, environ: Mapping[str, str] | None) -> str:
        value = (environ or __import__("os").environ).get(ref.name) if ref.kind == "env" else None
        if ref.kind == "file":
            try:
                value = Path(ref.name).read_text(encoding="utf-8", newline="")
            except OSError:
                value = None
            if type(value) is str:
                if value.endswith("\r\n"):
                    value = value[:-2]
                elif value.endswith("\n"):
                    value = value[:-1]
        if (
            type(value) is not str
            or (ref.kind == "file" and not value)
            or value != value.strip()
            or (
                ref.kind == "file"
                and any(unicodedata.category(character) == "Cc" for character in value)
            )
        ):
            raise ValueError("notification endpoint is unavailable")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "@" in parsed.netloc
            or parsed.fragment
        ):
            raise ValueError("notification endpoint is invalid")
        return value

    @staticmethod
    def _hash(value: object) -> str:
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
