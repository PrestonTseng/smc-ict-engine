from __future__ import annotations

import json
from email.message import Message
from typing import Any, cast
from urllib.error import HTTPError

import pytest

from smc_ict.application.ports import NotificationEvent
from smc_ict.configuration.models import (
    BatchingConfig,
    DeduplicationConfig,
    NotificationDestination,
    RedactionConfig,
    RetryConfig,
    SecretRef,
)


def _destination(*, attempts: int = 1, maximum_events: int = 10) -> NotificationDestination:
    return NotificationDestination(
        "discord_webhook",
        True,
        ("run_started", "run_succeeded", "run_failed", "decision_found", "no_decision"),
        SecretRef("env", "DISCORD_HOOK"),
        2,
        RetryConfig(attempts, tuple(range(1, attempts))),
        DeduplicationConfig(300, ("event_type", "run_id", "instrument_id")),
        BatchingConfig(maximum_events, 2),
        RedactionConfig(("authorization",), ("token",)),
        "warning",
    )


@pytest.mark.parametrize(
    ("event_type", "expected_title", "expected_color"),
    [
        ("run_started", "Run started", 0x3498DB),
        ("run_succeeded", "Run succeeded", 0x2ECC71),
        ("run_failed", "Run failed", 0xE74C3C),
        ("decision_found", "Decision ready", 0x9B59B6),
        ("no_decision", "No decision", 0x95A5A6),
    ],
)
def test_discord_formatter_uses_stable_native_embed_for_each_event(
    event_type: str, expected_title: str, expected_color: int
) -> None:
    from smc_ict.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        event_type,
        "run-1",
        "BTC-USDT-PERP" if event_type in {"decision_found", "no_decision"} else None,
        "source-aligned-research",
        1,
        {"status": "READY" if event_type == "decision_found" else "SUCCEEDED"},
    )

    payload = format_discord_payload((event,))

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == expected_title
    assert embed["color"] == expected_color
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Run ID"] == "run-1"
    assert fields["Strategy"] == "source-aligned-research"
    assert fields["Status"] == ("READY" if event_type == "decision_found" else "SUCCEEDED")


def test_discord_formatter_bounds_embeds_fields_and_disables_mentions() -> None:
    from smc_ict.adapters.notifications.discord_webhook import format_discord_payload

    injected = '@everyone <@123> "quoted"\n' + "x" * 5_000
    events = tuple(
        NotificationEvent(
            "run_failed",
            f"run-{index}-{injected}",
            None,
            injected,
            1,
            {f"untrusted_{field}_{injected}": injected for field in range(40)},
        )
        for index in range(10)
    )

    payload = format_discord_payload(events)
    encoded = json.dumps(payload)
    embeds = cast(list[dict[str, Any]], payload["embeds"])

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(embeds) == 10
    assert "\\n" in encoded and '\\"quoted\\"' in encoded
    assert (
        sum(
            len(embed["title"])
            + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
            for embed in embeds
        )
        <= 6_000
    )
    assert all(len(embed["title"]) <= 256 for embed in embeds)
    assert all(len(embed["fields"]) <= 25 for embed in embeds)
    assert all(
        len(field["name"]) <= 256 and len(field["value"]) <= 1_024
        for embed in embeds
        for field in embed["fields"]
    )

    with pytest.raises(ValueError, match="at most 10"):
        format_discord_payload(events + events[:1])


def test_discord_formatter_renders_ready_decision_debug_evidence() -> None:
    from smc_ict.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "decision_found",
        "run-1",
        "BTC-USDT-PERP",
        "source-aligned-research",
        1,
        {
            "status": "READY",
            "direction": "LONG",
            "evaluation_time_ms": 900_000,
            "closed_bar_time_ms": 899_999,
            "entry": "101.5",
            "stop": "99.25",
            "target": "106",
            "reward_risk": "2",
            "decision_id": "a" * 64,
        },
    )

    embed = cast(list[dict[str, Any]], format_discord_payload((event,))["embeds"])[0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields == {
        "Run ID": "run-1",
        "Strategy": "source-aligned-research",
        "Instrument": "BTC-USDT-PERP",
        "Closed bar time (ms)": "899999",
        "Decision ID": "a" * 64,
        "Direction": "LONG",
        "Entry": "101.5",
        "Evaluation time (ms)": "900000",
        "Reward/risk": "2",
        "Status": "READY",
        "Stop": "99.25",
        "Target": "106",
    }


@pytest.mark.parametrize("status", ["NO_TRADE", "UNAVAILABLE"])
def test_discord_formatter_renders_no_decision_failure_evidence(status: str) -> None:
    from smc_ict.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "no_decision",
        "run-1",
        "BTC-USDT-PERP",
        "source-aligned-research",
        1,
        {
            "status": status,
            "evaluation_time_ms": 900_000,
            "closed_bar_time_ms": 899_999,
            "first_failed_signal": "ict.fair_value_gap",
            "decision_id": "b" * 64,
        },
    )

    embed = cast(list[dict[str, Any]], format_discord_payload((event,))["embeds"])[0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["Status"] == status
    assert fields["First failed signal"] == "ict.fair_value_gap"
    assert fields["Decision ID"] == "b" * 64


def test_discord_adapter_posts_native_json_and_accepts_204() -> None:
    from smc_ict.adapters.notifications.discord_webhook import DiscordWebhookNotifier

    requests: list[Any] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    def opened(request: Any, *, timeout: float) -> Response:
        requests.append(request)
        assert timeout == 2
        return Response()

    receipt = DiscordWebhookNotifier(
        "discord_debug",
        _destination(),
        environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
        opener=opened,
        clock_seconds=lambda: 10,
    ).deliver(NotificationEvent("run_started", "run-1", None, "strategy", 1, {"status": "RUNNING"}))

    assert receipt.outcome == "SUCCESS"
    assert receipt.status_code == 204
    assert receipt.adapter_id == "discord_webhook"
    assert requests[0].full_url == "https://discord.invalid/api/webhooks/id/token"
    assert requests[0].headers["Content-type"] == "application/json"
    assert json.loads(requests[0].data) == {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "color": 0x3498DB,
                "fields": [
                    {"inline": True, "name": "Run ID", "value": "run-1"},
                    {"inline": True, "name": "Strategy", "value": "strategy"},
                    {"inline": True, "name": "Status", "value": "RUNNING"},
                ],
                "title": "Run started",
            }
        ],
    }


def test_discord_adapter_honors_rate_limit_then_retries_server_failure() -> None:
    from smc_ict.adapters.notifications.discord_webhook import DiscordWebhookNotifier

    attempts = [0]
    sleeps: list[int] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    def opened(*_args: object, **_kwargs: object) -> Response:
        attempts[0] += 1
        if attempts[0] == 1:
            headers = Message()
            headers["Retry-After"] = "3"
            raise HTTPError("https://discord.invalid", 429, "rate limited", headers, None)
        if attempts[0] == 2:
            raise HTTPError("https://discord.invalid", 500, "server error", Message(), None)
        return Response()

    receipt = DiscordWebhookNotifier(
        "discord_debug",
        _destination(attempts=3),
        environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
        opener=opened,
        sleeper=sleeps.append,
    ).deliver(NotificationEvent("run_succeeded", "run-1", None, "strategy", 1, {}))

    assert receipt.outcome == "SUCCESS"
    assert receipt.attempts == 3
    assert sleeps == [3, 2]


def test_discord_adapter_does_not_retry_non_retryable_failure() -> None:
    from smc_ict.adapters.notifications.discord_webhook import DiscordWebhookNotifier

    attempts = [0]
    sleeps: list[int] = []

    def opened(*_args: object, **_kwargs: object) -> None:
        attempts[0] += 1
        raise HTTPError("https://discord.invalid", 400, "bad request", Message(), None)

    receipt = DiscordWebhookNotifier(
        "discord_debug",
        _destination(attempts=3),
        environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
        opener=opened,
        sleeper=sleeps.append,
    ).deliver(NotificationEvent("run_failed", "run-1", None, "strategy", 1, {}))

    assert receipt.outcome == "FAILURE"
    assert receipt.reason_code == "HTTP_400"
    assert receipt.attempts == 1
    assert attempts == [1]
    assert sleeps == []


def test_discord_adapter_rejects_batch_above_destination_bound_before_transport() -> None:
    from smc_ict.adapters.notifications.discord_webhook import DiscordWebhookNotifier

    def opened(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid batch must not reach transport")

    adapter = DiscordWebhookNotifier(
        "discord_debug",
        _destination(maximum_events=1),
        environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
        opener=opened,
    )
    event = NotificationEvent("run_started", "run-1", None, "strategy", 1, {})

    with pytest.raises(ValueError, match="configured bounds"):
        adapter.deliver_batch((event, event))
