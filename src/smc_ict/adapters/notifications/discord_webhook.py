"""Discord-native webhook payload formatting and delivery."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from time import sleep, time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from smc_ict.application.ports.notifications import DeliveryReceipt, NotificationEvent
from smc_ict.configuration.models import NotificationDestination

from .generic_webhook import GenericWebhookNotifier

_USER_AGENT = "smc-ict-engine/0.1 discord-webhook"
_EVENT_PRESENTATION = {
    "run_started": ("Run started", 0x3498DB),
    "run_succeeded": ("Run succeeded", 0x2ECC71),
    "run_failed": ("Run failed", 0xE74C3C),
    "decision_found": ("Decision ready", 0x9B59B6),
    "no_decision": ("No decision", 0x95A5A6),
}
_FIELD_LABELS = {
    "closed_bar_time_ms": "Closed bar time (ms)",
    "decision_count": "Decision count",
    "decision_id": "Decision ID",
    "direction": "Direction",
    "entry": "Entry",
    "error_category": "Error category",
    "evaluation_time_ms": "Evaluation time (ms)",
    "first_failed_signal": "First failed signal",
    "instrument_count": "Instrument count",
    "reward_risk": "Reward/risk",
    "status": "Status",
    "stop": "Stop",
    "target": "Target",
}


def _truncate(value: object, maximum: int) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    if maximum == 1:
        return "…"
    return text[: maximum - 1] + "…"


def _embed(event: NotificationEvent, *, character_budget: int) -> dict[str, object]:
    title, color = _EVENT_PRESENTATION[event.event_type]
    values: list[tuple[str, object]] = [
        ("Run ID", event.run_id),
        ("Strategy", event.strategy_id),
    ]
    if event.instrument_id is not None:
        values.append(("Instrument", event.instrument_id))
    values.extend(
        (_FIELD_LABELS.get(name, name.replace("_", " ").title()), value)
        for name, value in sorted(event.payload.items())
        if value is not None
    )
    maximum_fields = min(25, max(3, character_budget // 48))
    values = values[:maximum_fields]
    names = [_truncate(name, 24) for name, _value in values]
    value_budget = max(len(values), character_budget - len(title) - sum(map(len, names)))
    value_limit = max(1, min(1_024, value_budget // len(values)))
    fields = [
        {"name": name, "value": _truncate(value, value_limit), "inline": True}
        for name, (_label, value) in zip(names, values, strict=True)
    ]
    return {"title": title, "color": color, "fields": fields}


def format_discord_payload(events: tuple[NotificationEvent, ...]) -> dict[str, object]:
    """Create a native Discord body without resolving or exposing an endpoint."""

    if not events:
        raise ValueError("Discord payload requires at least one event")
    if len(events) > 10:
        raise ValueError("Discord payload supports at most 10 events")
    character_budget = 6_000 // len(events)
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [_embed(event, character_budget=character_budget) for event in events],
    }


class DiscordWebhookNotifier:
    """Deliver bounded native Discord webhook messages."""

    adapter_id = "discord_webhook"

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
        self._endpoint = GenericWebhookNotifier._resolve(destination.endpoint, environ)
        self._opener = opener
        self._sleeper = sleeper
        self._clock_seconds = clock_seconds

    def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
        return self._deliver((event,))

    def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        if not 1 <= len(events) <= self._destination.batching.maximum_events:
            raise ValueError("notification batch size is outside configured bounds")
        return self._deliver(events)

    def _deliver(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        body = json.dumps(
            format_discord_payload(events), sort_keys=True, separators=(",", ":")
        ).encode()
        event_id = GenericWebhookNotifier._hash(
            [(event.event_type, event.run_id, event.instrument_id) for event in events]
        )
        deduplication_id = GenericWebhookNotifier._hash(
            {"destination_id": self._destination_id, "event_id": event_id}
        )
        batch_id = GenericWebhookNotifier._hash(
            {
                "destination_id": self._destination_id,
                "window": self._clock_seconds() // self._destination.batching.flush_seconds,
            }
        )
        attempt = 0
        status: int | None = None
        reason = "TRANSPORT_ERROR"
        for attempt in range(1, self._destination.retries.maximum_attempts + 1):
            retry_after: int | None = None
            try:
                request = Request(
                    self._endpoint,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": _USER_AGENT,
                    },
                    method="POST",
                )
                with self._opener(  # type: ignore[attr-defined]
                    request, timeout=float(self._destination.timeout_seconds)
                ) as response:
                    status = response.getcode()
                if status is None:
                    raise OSError("Discord webhook response omitted status")
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
                if status == 429:
                    try:
                        retry_after = int(exc.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        retry_after = None
                    if retry_after is not None and not 1 <= retry_after <= 300:
                        retry_after = None
            except (OSError, URLError, TimeoutError):
                status = None
                reason = "TRANSPORT_ERROR"
            retryable = status is None or status in {408, 429} or status >= 500
            if not retryable or attempt >= self._destination.retries.maximum_attempts:
                break
            delay = retry_after or self._destination.retries.backoff_seconds[attempt - 1]
            self._sleeper(delay)
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
