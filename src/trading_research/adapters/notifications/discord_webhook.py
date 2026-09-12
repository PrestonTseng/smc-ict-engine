"""Discord-native webhook payload formatting and delivery."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import sleep, time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from trading_research.application.ports.notifications import DeliveryReceipt, NotificationEvent
from trading_research.configuration.models import NotificationDestination

from .generic_webhook import GenericWebhookNotifier

_USER_AGENT = "trading-research-engine/0.2.0 discord-webhook"
_LIFECYCLE_PRESENTATION = {
    "run_started": ("▶ Evaluation started", 0x3498DB),
    "run_succeeded": ("✓ Evaluation complete", 0x2ECC71),
    "run_failed": ("⚠ Evaluation failed", 0xE74C3C),
}
_LIFECYCLE_STATUS = {
    "run_started": "RUNNING",
    "run_succeeded": "SUCCEEDED",
    "run_failed": "FAILED",
}


def _truncate(value: object, maximum: int) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    if maximum == 1:
        return "…"
    return text[: maximum - 1] + "…"


def _prefix(value: object) -> str:
    return str(value)[:8]


def _timestamp(milliseconds: int) -> str:
    value = datetime.fromtimestamp(milliseconds / 1_000, UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_error_category(value: object) -> str:
    if type(value) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        return value
    return "Failure details unavailable"


def _exact_decision_value(value: object) -> str:
    if type(value) is not str:
        return "Unavailable"
    if len(value) > 64:
        raise ValueError("exact decision value exceeds 64 characters")
    return value


def _lifecycle_embed(event: NotificationEvent) -> dict[str, object]:
    title, color = _LIFECYCLE_PRESENTATION[event.event_type]
    status = _LIFECYCLE_STATUS[event.event_type]
    event_time_ms = event.payload.get("event_time_ms")
    fields = [
        {"name": "Strategy", "value": _truncate(event.strategy_id, 128), "inline": True},
        {
            "name": "Instruments",
            "value": _truncate(event.payload.get("instrument_count", "Unknown"), 32),
            "inline": True,
        },
    ]
    if event.event_type == "run_failed":
        fields.append(
            {
                "name": "Error category",
                "value": _safe_error_category(event.payload.get("error_category")),
                "inline": False,
            }
        )
    embed: dict[str, object] = {
        "title": title,
        "description": f"**{status}** · Evaluation lifecycle update.",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Run {_prefix(event.run_id)} · Schema v{event.payload_schema_version}"},
    }
    if type(event_time_ms) is int:
        seconds = event_time_ms // 1_000
        fields.append(
            {
                "name": "Event time",
                "value": f"<t:{seconds}:F> · <t:{seconds}:R>",
                "inline": False,
            }
        )
        embed["timestamp"] = _timestamp(event_time_ms)
    return embed


def _decision_embed(event: NotificationEvent) -> dict[str, object]:
    direction = event.payload.get("direction")
    direction_label = direction if direction in {"LONG", "SHORT"} else "READY"
    instrument = event.instrument_id or "Unknown instrument"
    closed_bar_time_ms = event.payload.get("closed_bar_time_ms")
    fields = [
        {
            "name": label,
            "value": _exact_decision_value(event.payload.get(key)),
            "inline": True,
        }
        for label, key in (
            ("Entry", "entry"),
            ("Stop", "stop"),
            ("Target", "target"),
            ("Reward/risk", "reward_risk"),
        )
    ]
    fields.extend(
        (
            {"name": "Strategy", "value": _truncate(event.strategy_id, 128), "inline": False},
            {"name": "Reason", "value": "Setup criteria satisfied", "inline": False},
        )
    )
    embed: dict[str, object] = {
        "title": _truncate(f"🎯 {direction_label} setup · {instrument}", 256),
        "description": "**READY** · Setup criteria satisfied.",
        "color": 0xF1C40F,
        "fields": fields,
        "footer": {
            "text": (
                f"Run {_prefix(event.run_id)} · Decision "
                f"{_prefix(event.payload.get('decision_id', 'unknown'))} · "
                f"Schema v{event.payload_schema_version}"
            )
        },
    }
    if type(closed_bar_time_ms) is int:
        fields.append(
            {
                "name": "Closed bar",
                "value": f"<t:{closed_bar_time_ms // 1_000}:F>",
                "inline": False,
            }
        )
        embed["timestamp"] = _timestamp(closed_bar_time_ms)
    return embed


def _safe_reason(value: object) -> str:
    if type(value) is str and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return "Reason unavailable"


def _no_decision_embed(event: NotificationEvent) -> dict[str, object]:
    status = event.payload.get("status")
    status_label = status if status in {"NO_TRADE", "UNAVAILABLE"} else "NO_SETUP"
    explanation = (
        "No configured setup passed."
        if status_label == "NO_TRADE"
        else "Required evidence was unavailable."
        if status_label == "UNAVAILABLE"
        else "No setup result was available."
    )
    instrument = event.instrument_id or "Unknown instrument"
    reason = _safe_reason(event.payload.get("first_failed_signal"))
    closed_bar_time_ms = event.payload.get("closed_bar_time_ms")
    fields = [
        {"name": "Strategy", "value": _truncate(event.strategy_id, 128), "inline": False},
        {"name": "Reason", "value": reason, "inline": False},
    ]
    embed: dict[str, object] = {
        "title": _truncate(f"No setup · {instrument}", 256),
        "description": f"**{status_label}** · {explanation}",
        "color": 0x95A5A6,
        "fields": fields,
        "footer": {
            "text": (
                f"Run {_prefix(event.run_id)} · Decision "
                f"{_prefix(event.payload.get('decision_id', 'unknown'))} · "
                f"Schema v{event.payload_schema_version}"
            )
        },
    }
    if type(closed_bar_time_ms) is int:
        fields.append(
            {
                "name": "Closed bar",
                "value": f"<t:{closed_bar_time_ms // 1_000}:F>",
                "inline": False,
            }
        )
        embed["timestamp"] = _timestamp(closed_bar_time_ms)
    return embed


def _can_aggregate_no_decisions(events: tuple[NotificationEvent, ...]) -> bool:
    first = events[0]
    boundary = (first.run_id, first.strategy_id, first.payload.get("closed_bar_time_ms"))
    return len(events) > 1 and all(
        event.event_type == "no_decision"
        and (event.run_id, event.strategy_id, event.payload.get("closed_bar_time_ms")) == boundary
        for event in events
    )


def _no_decision_summary(events: tuple[NotificationEvent, ...]) -> dict[str, object]:
    first = events[0]
    statuses = Counter(
        status if status in {"NO_TRADE", "UNAVAILABLE"} else "NO_SETUP"
        for status in (event.payload.get("status") for event in events)
    )
    description = " · ".join(
        f"**{status}** {statuses[status]}"
        for status in ("NO_TRADE", "UNAVAILABLE", "NO_SETUP")
        if statuses[status]
    )
    rules = Counter(_safe_reason(event.payload.get("first_failed_signal")) for event in events)
    rule_summary = "\n".join(
        f"{rule} \N{MULTIPLICATION SIGN}{count}" for rule, count in sorted(rules.items())
    )
    instruments = "\n".join(sorted(event.instrument_id or "Unknown instrument" for event in events))
    closed_bar_time_ms = first.payload.get("closed_bar_time_ms")
    fields = [
        {"name": "Failed rules", "value": _truncate(rule_summary, 180), "inline": False},
        {"name": "Instruments", "value": _truncate(instruments, 180), "inline": False},
        {"name": "Strategy", "value": _truncate(first.strategy_id, 96), "inline": False},
    ]
    embed: dict[str, object] = {
        "title": f"No setup · {len(events)} instruments",
        "description": description,
        "color": 0x95A5A6,
        "fields": fields,
        "footer": {"text": f"Run {_prefix(first.run_id)} · Schema v{first.payload_schema_version}"},
    }
    if type(closed_bar_time_ms) is int:
        fields.append(
            {
                "name": "Closed bar",
                "value": f"<t:{closed_bar_time_ms // 1_000}:F>",
                "inline": False,
            }
        )
        embed["timestamp"] = _timestamp(closed_bar_time_ms)
    return embed


def _embed(event: NotificationEvent) -> dict[str, object]:
    if event.event_type in {"run_started", "run_succeeded", "run_failed"}:
        return _lifecycle_embed(event)
    if event.event_type == "decision_found":
        return _decision_embed(event)
    if event.event_type == "no_decision":
        return _no_decision_embed(event)
    raise ValueError("unsupported Discord event type")


def format_discord_payload(
    events: tuple[NotificationEvent, ...], *, part_number: int | None = None
) -> dict[str, object]:
    """Create a native Discord body without resolving or exposing an endpoint."""

    if not events:
        raise ValueError("Discord payload requires at least one event")
    if len(events) > 8:
        raise ValueError("Discord payload supports at most 8 events")
    embeds = (
        [_no_decision_summary(events)]
        if _can_aggregate_no_decisions(events)
        else [_embed(event) for event in events]
    )
    payload: dict[str, object] = {
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
    }
    if part_number is not None:
        payload["content"] = f"Evaluation results · Part {part_number}"
    return payload


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
        self._terminal_parts: dict[tuple[str, object], int] = {}

    def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
        return self._deliver((event,))

    def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        if not 1 <= len(events) <= self._destination.batching.maximum_events:
            raise ValueError("notification batch size is outside configured bounds")
        return self._deliver(events)

    def _deliver(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        part_number: int | None = None
        if all(event.event_type in {"decision_found", "no_decision"} for event in events):
            boundary = (events[0].run_id, events[0].payload.get("closed_bar_time_ms"))
            part_number = self._terminal_parts.get(boundary, 0) + 1
            self._terminal_parts[boundary] = part_number
        body = json.dumps(
            format_discord_payload(events, part_number=part_number),
            sort_keys=True,
            separators=(",", ":"),
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
