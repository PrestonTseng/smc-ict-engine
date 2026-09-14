from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from trading_research.application.ports import NotificationEvent
from trading_research.configuration.models import (
    BatchingConfig,
    DeduplicationConfig,
    NotificationDestination,
    RedactionConfig,
    RetryConfig,
    SecretRef,
)


def _destination(*, attempts: int = 1, maximum_events: int = 8) -> NotificationDestination:
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
    ("event_type", "status", "expected_title", "expected_color"),
    [
        ("run_started", "RUNNING", "▶ Evaluation started", 0x3498DB),
        ("run_succeeded", "SUCCEEDED", "✓ Evaluation complete", 0x2ECC71),
        ("run_failed", "FAILED", "⚠ Evaluation failed", 0xE74C3C),
    ],
)
def test_discord_formatter_renders_human_first_lifecycle_card(
    event_type: str, status: str, expected_title: str, expected_color: int
) -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        event_type,
        "run-1",
        None,
        "source-aligned-research",
        1,
        {"status": status, "event_time_ms": 1_725_000_000_123, "instrument_count": 2},
    )

    payload = format_discord_payload((event,))

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == expected_title
    assert embed["color"] == expected_color
    assert status in embed["description"]
    assert embed["timestamp"] == "2024-08-30T06:40:00.123Z"
    assert "1725000000123" not in json.dumps(embed)
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Event time"] == "<t:1725000000:F> · <t:1725000000:R>"
    assert fields["Strategy"] == "source-aligned-research"
    assert fields["Instruments"] == "2"
    assert embed["footer"]["text"] == "Run run-1 · Schema v1"


def test_discord_formatter_bounds_embeds_fields_and_disables_mentions() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

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
        for index in range(8)
    )

    payload = format_discord_payload(events)
    encoded = json.dumps(payload)
    embeds = cast(list[dict[str, Any]], payload["embeds"])

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(embeds) == 8
    assert "\\n" in encoded and '\\"quoted\\"' in encoded
    assert (
        sum(
            len(embed["title"])
            + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
            for embed in embeds
        )
        <= 5_500
    )
    assert all(
        len(embed["title"])
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
        + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
        <= 650
        for embed in embeds
    )
    assert all(len(embed["title"]) <= 256 for embed in embeds)
    assert all(len(embed["fields"]) <= 25 for embed in embeds)
    assert all(
        len(field["name"]) <= 256 and len(field["value"]) <= 1_024
        for embed in embeds
        for field in embed["fields"]
    )

    with pytest.raises(ValueError, match="at most 8"):
        format_discord_payload(events + events[:1])


def test_discord_formatter_bounds_long_human_fields_without_reformatting_prices() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "decision_found",
        "1234567890abcdef",
        f"{'X' * 50}-USDT-PERP",
        "strategy-" + "界" * 1_000,
        1,
        {
            "status": "READY",
            "direction": "LONG",
            "closed_bar_time_ms": 1_725_000_000_999,
            "entry": "1" * 64,
            "stop": "2" * 64,
            "target": "3" * 64,
            "reward_risk": "4" * 64,
            "decision_id": "a" * 64,
        },
    )

    embed = cast(list[dict[str, Any]], format_discord_payload((event,))["embeds"])[0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    counted = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    )

    assert counted <= 650
    assert len(embed["title"]) <= 256
    assert all(len(field["name"]) <= 256 for field in embed["fields"])
    assert all(len(field["value"]) <= 1_024 for field in embed["fields"])
    assert fields["Entry"] == "1" * 64
    assert fields["Stop"] == "2" * 64
    assert fields["Target"] == "3" * 64
    assert fields["Reward/risk"] == "4" * 64


def test_discord_formatter_rejects_exact_price_that_cannot_fit_card_budget() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "decision_found",
        "run-1",
        "BTC-USDT-PERP",
        "strategy",
        1,
        {
            "status": "READY",
            "direction": "LONG",
            "closed_bar_time_ms": 1_725_000_000_999,
            "entry": "1" * 65,
            "stop": "99.250",
            "target": "106.000",
            "reward_risk": "2.000",
        },
    )

    with pytest.raises(ValueError, match="exact decision value exceeds 64 characters"):
        format_discord_payload((event,))


def test_discord_formatter_aggregates_one_run_and_bar_without_hiding_status_counts() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    events = tuple(
        NotificationEvent(
            "no_decision",
            "1234567890abcdef",
            instrument,
            "source-aligned-research",
            1,
            {
                "status": status,
                "closed_bar_time_ms": 1_725_000_000_999,
                "decision_id": decision_id * 64,
                "first_failed_signal": rule,
            },
        )
        for instrument, status, decision_id, rule in (
            ("BTC-USDT-PERP", "NO_TRADE", "a", "ict.fair_value_gap"),
            ("ETH-USDT-PERP", "UNAVAILABLE", "b", "decision.levels"),
            ("XRP-USDT-PERP", "NO_TRADE", "c", "ict.fair_value_gap"),
        )
    )

    payload = format_discord_payload(events)

    embeds = cast(list[dict[str, Any]], payload["embeds"])
    assert len(embeds) == 1
    embed = embeds[0]
    assert embed["title"] == "No setup · 3 instruments"
    assert embed["description"] == "**NO_TRADE** 2 · **UNAVAILABLE** 1"
    assert [(field["name"], field["value"]) for field in embed["fields"]] == [
        (
            "Failed rules",
            "decision.levels \N{MULTIPLICATION SIGN}1\nict.fair_value_gap \N{MULTIPLICATION SIGN}2",
        ),
        ("Instruments", "BTC-USDT-PERP\nETH-USDT-PERP\nXRP-USDT-PERP"),
        ("Strategy", "source-aligned-research"),
        ("Closed bar", "<t:1725000000:F>"),
    ]
    assert embed["footer"]["text"] == "Run 12345678 · Schema v1"


def test_discord_formatter_bounds_eight_event_no_setup_summary() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    events = tuple(
        NotificationEvent(
            "no_decision",
            "1234567890abcdef",
            f"{'X' * 48}{index}-USDT-PERP",
            "strategy-" + "界" * 1_000,
            1,
            {
                "status": "NO_TRADE" if index % 2 == 0 else "UNAVAILABLE",
                "closed_bar_time_ms": 1_725_000_000_999,
                "first_failed_signal": f"rule.{index}.{'x' * 110}",
            },
        )
        for index in range(8)
    )

    embed = cast(list[dict[str, Any]], format_discord_payload(events)["embeds"])[0]
    counted = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    )

    assert counted <= 650
    assert "**NO_TRADE** 4" in embed["description"]
    assert "**UNAVAILABLE** 4" in embed["description"]


def test_discord_formatter_renders_ready_decision_as_exact_human_setup() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "decision_found",
        "run-1",
        "BTC-USDT-PERP",
        "source-aligned-research",
        1,
        {
            "status": "READY",
            "direction": "LONG",
            "evaluation_time_ms": 1_725_000_000_999,
            "closed_bar_time_ms": 1_725_000_000_999,
            "entry": "101.5000",
            "stop": "99.250",
            "target": "106.000",
            "reward_risk": "2.000",
            "decision_id": "a" * 64,
        },
    )

    embed = cast(list[dict[str, Any]], format_discord_payload((event,))["embeds"])[0]
    assert embed["title"] == "🎯 LONG setup · BTC-USDT-PERP"
    assert embed["color"] == 0xF1C40F
    assert embed["description"] == "**READY** · Setup criteria satisfied."
    assert embed["timestamp"] == "2024-08-30T06:40:00.999Z"
    fields = [(field["name"], field["value"]) for field in embed["fields"]]

    assert fields == [
        ("Entry", "101.5000"),
        ("Stop", "99.250"),
        ("Target", "106.000"),
        ("Reward/risk", "2.000"),
        ("Strategy", "source-aligned-research"),
        ("Reason", "Setup criteria satisfied"),
        ("Closed bar", "<t:1725000000:F>"),
    ]
    assert embed["footer"]["text"] == "Run run-1 · Decision aaaaaaaa · Schema v1"
    visible = json.dumps(embed, ensure_ascii=False)
    assert "1725000000999" not in visible
    assert "a" * 64 not in visible


@pytest.mark.parametrize("status", ["NO_TRADE", "UNAVAILABLE"])
def test_discord_formatter_renders_no_setup_status_and_reason(status: str) -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    event = NotificationEvent(
        "no_decision",
        "run-1",
        "BTC-USDT-PERP",
        "source-aligned-research",
        1,
        {
            "status": status,
            "evaluation_time_ms": 1_725_000_000_999,
            "closed_bar_time_ms": 1_725_000_000_999,
            "first_failed_signal": "ict.fair_value_gap",
            "decision_id": "b" * 64,
        },
    )

    embed = cast(list[dict[str, Any]], format_discord_payload((event,))["embeds"])[0]
    assert embed["title"] == "No setup · BTC-USDT-PERP"
    assert embed["color"] == 0x95A5A6
    assert embed["description"].startswith(f"**{status}** ·")
    assert embed["timestamp"] == "2024-08-30T06:40:00.999Z"
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields == {
        "Strategy": "source-aligned-research",
        "Reason": "ict.fair_value_gap",
        "Closed bar": "<t:1725000000:F>",
    }
    assert embed["footer"]["text"] == "Run run-1 · Decision bbbbbbbb · Schema v1"
    visible = json.dumps(embed, ensure_ascii=False)
    assert "1725000000999" not in visible
    assert "b" * 64 not in visible


def test_discord_formatter_replaces_untrusted_reason_and_error_details() -> None:
    from trading_research.adapters.notifications.discord_webhook import format_discord_payload

    no_setup = NotificationEvent(
        "no_decision",
        "run-secret-value",
        "BTC-USDT-PERP",
        "strategy",
        1,
        {
            "status": "unexpected",
            "closed_bar_time_ms": 1_725_000_000_999,
            "first_failed_signal": "https://secret.invalid/token?key=hidden\n" + "x" * 500,
        },
    )
    failed = NotificationEvent(
        "run_failed",
        "run-secret-value",
        None,
        "strategy",
        1,
        {
            "status": "https://secret.invalid/status?token=hidden",
            "event_time_ms": 1_725_000_000_999,
            "error_category": "https://secret.invalid/token?key=hidden",
        },
    )

    embeds = cast(list[dict[str, Any]], format_discord_payload((no_setup, failed))["embeds"])
    encoded = json.dumps(embeds)

    assert embeds[0]["description"].startswith("**NO_SETUP**")
    assert {field["name"]: field["value"] for field in embeds[0]["fields"]}["Reason"] == (
        "Reason unavailable"
    )
    assert {field["name"]: field["value"] for field in embeds[1]["fields"]}[
        "Error category"
    ] == "Failure details unavailable"
    assert embeds[1]["description"].startswith("**FAILED**")
    assert "secret.invalid" not in encoded
    assert "hidden" not in encoded


def test_discord_adapter_posts_native_json_and_accepts_204() -> None:
    from trading_research.adapters.notifications.discord_webhook import (
        DiscordWebhookNotifier,
        format_discord_payload,
    )

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
    assert json.loads(requests[0].data) == format_discord_payload(
        (NotificationEvent("run_started", "run-1", None, "strategy", 1, {"status": "RUNNING"}),)
    )


def test_discord_adapter_user_agent_closes_fake_https_403_to_204_differential() -> None:
    from trading_research.adapters.notifications.discord_webhook import DiscordWebhookNotifier

    expected_user_agent = "trading-research-engine/0.2.0 discord-webhook"
    observed_user_agents: list[str | None] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    def fake_discord_https(request: Request, *, timeout: float = 2) -> Response:
        assert timeout == 2
        user_agent = request.get_header("User-agent")
        observed_user_agents.append(user_agent)
        if user_agent != expected_user_agent:
            raise HTTPError(request.full_url, 403, "forbidden", Message(), None)
        return Response()

    endpoint = "https://discord.invalid/api/webhooks/id/token"
    with pytest.raises(HTTPError) as missing_header:
        fake_discord_https(Request(endpoint, data=b"{}", method="POST"))

    receipt = DiscordWebhookNotifier(
        "discord_debug",
        _destination(),
        environ={"DISCORD_HOOK": endpoint},
        opener=fake_discord_https,
    ).deliver(NotificationEvent("run_started", "run-1", None, "strategy", 1, {}))

    assert missing_header.value.code == 403
    assert receipt.outcome == "SUCCESS"
    assert receipt.status_code == 204
    assert receipt.attempts == 1
    assert observed_user_agents == [None, expected_user_agent]
    assert "token" not in expected_user_agent


def test_discord_adapter_honors_rate_limit_then_retries_server_failure() -> None:
    from trading_research.adapters.notifications.discord_webhook import DiscordWebhookNotifier

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
    from trading_research.adapters.notifications.discord_webhook import DiscordWebhookNotifier

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
    from trading_research.adapters.notifications.discord_webhook import DiscordWebhookNotifier

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


def test_notification_destination_limits_discord_to_eight_but_generic_to_one_thousand() -> None:
    discord = _destination(maximum_events=8)

    assert discord.batching.maximum_events == 8
    with pytest.raises(ValueError, match=r"maximum_events.*1\.\.8"):
        _destination(maximum_events=9)

    generic = discord.model_copy(
        update={"adapter": "generic_webhook", "batching": BatchingConfig(1_000, 2)}
    )
    assert generic.batching.maximum_events == 1_000


def test_loaded_discord_config_routes_only_formatter_compatible_batches() -> None:
    from trading_research.application.notifications import NotificationRouter
    from trading_research.application.ports import Notifier
    from trading_research.composition.registries import notification_composition_root
    from trading_research.configuration import load_notifications_text

    source = (
        Path("config/notifications.yaml")
        .read_text(encoding="utf-8")
        .replace(
            "endpoint:\n        file: /run/secrets/discord_webhook_url",
            "endpoint: {env: DISCORD_HOOK}",
        )
    )
    config = load_notifications_text(source)
    root = notification_composition_root()
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
        assert timeout == 5
        return Response()

    def adapter_factory(destination_id: str, destination: NotificationDestination) -> Notifier:
        candidate = root.notifiers.resolve(destination.adapter)(
            destination_id,
            destination,
            environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
            opener=opened,
            clock_seconds=lambda: 1,
        )
        assert isinstance(candidate, Notifier)
        return candidate

    router = NotificationRouter(config, adapter_factory=adapter_factory, clock_seconds=lambda: 1)
    events = tuple(
        NotificationEvent(
            "decision_found",
            "run-1",
            f"I{index}-USDT-PERP",
            "strategy",
            1,
            {"status": "READY", "closed_bar_time_ms": 1_725_000_000_999},
        )
        for index in range(9)
    )

    first = router.deliver_all(events)
    final = router.close()

    assert first.outcome == "ALL_SUCCESS"
    assert final.outcome == "ALL_SUCCESS"
    assert [len(json.loads(request.data)["embeds"]) for request in requests] == [8, 1]
    assert [json.loads(request.data)["content"] for request in requests] == [
        "Evaluation results · Part 1",
        "Evaluation results · Part 2",
    ]


@pytest.mark.parametrize(
    "payload_schema_version",
    [True, 0, 2_147_483_648, "1", 10**1_000],
)
def test_notification_event_rejects_noncanonical_or_unbounded_schema_version(
    payload_schema_version: object,
) -> None:
    with pytest.raises(ValueError, match="payload schema version"):
        NotificationEvent(
            "run_started",
            "run-1",
            None,
            "strategy",
            payload_schema_version,  # type: ignore[arg-type]
            {},
        )


@pytest.mark.parametrize("payload_schema_version", [1, 2_147_483_647])
def test_notification_event_accepts_schema_version_contract_boundaries(
    payload_schema_version: int,
) -> None:
    event = NotificationEvent("run_started", "run-1", None, "strategy", payload_schema_version, {})

    assert event.payload_schema_version == payload_schema_version


def test_eight_minimal_lifecycle_events_that_previously_rendered_8880_characters_are_rejected() -> (
    None
):
    # Eight two-character strategies and 1,000-digit footer versions previously rendered
    # 8,880 characters.
    with pytest.raises(ValueError, match="payload schema version"):
        tuple(
            NotificationEvent(
                "run_started",
                f"run-{index}",
                None,
                "st",
                10**999,
                {},
            )
            for index in range(8)
        )


@pytest.mark.parametrize(("visible_characters", "accepted"), [(5_500, True), (5_501, False)])
def test_discord_formatter_enforces_aggregate_visible_text_boundary(
    monkeypatch: pytest.MonkeyPatch, visible_characters: int, accepted: bool
) -> None:
    import trading_research.adapters.notifications.discord_webhook as discord

    event = NotificationEvent("run_started", "run-1", None, "strategy", 1, {})
    content = "Evaluation results · Part 1"
    per_embed, remainder = divmod(visible_characters - len(content), 8)
    embeds = iter(
        {"title": "x" * (per_embed + (1 if index < remainder else 0))} for index in range(8)
    )
    monkeypatch.setattr(discord, "_embed", lambda _event: next(embeds))

    if accepted:
        payload = discord.format_discord_payload((event,) * 8, part_number=1)
        assert discord._visible_text_character_count(payload) == 5_500
    else:
        with pytest.raises(ValueError, match="5,500"):
            discord.format_discord_payload((event,) * 8, part_number=1)


def test_discord_adapter_rejects_aggregate_overflow_before_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trading_research.adapters.notifications.discord_webhook as discord

    opened = False

    def opener(*_args: object, **_kwargs: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("aggregate overflow must not reach transport")

    monkeypatch.setattr(discord, "_embed", lambda _event: {"title": "x" * 5_501})
    adapter = discord.DiscordWebhookNotifier(
        "discord_debug",
        _destination(),
        environ={"DISCORD_HOOK": "https://discord.invalid/api/webhooks/id/token"},
        opener=opener,
    )

    with pytest.raises(ValueError, match="5,500"):
        adapter.deliver(NotificationEvent("run_started", "run-1", None, "strategy", 1, {}))

    assert opened is False
