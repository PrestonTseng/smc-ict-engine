from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest


def test_generic_webhook_resolves_secret_at_adapter_boundary_retries_and_redacts_errors() -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.application.ports import NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found",),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(2, (1,)),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(10, 2),
        RedactionConfig(("authorization",), ("token",)),
        "warning",
    )
    requests: list[object] = []
    sleeps: list[int] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    attempts = [0]

    def open_request(request: object, *, timeout: float) -> Response:
        attempts[0] += 1
        requests.append(request)
        if attempts[0] == 1:
            raise HTTPError("https://example.invalid/path?token=hidden", 500, "failure", None, None)
        assert timeout == 1
        return Response()

    adapter = GenericWebhookNotifier(
        "alpha",
        destination,
        environ={"HOOK": "https://example.invalid/path?token=hidden"},
        opener=open_request,
        sleeper=sleeps.append,
        clock_seconds=lambda: 10,
    )
    receipt = adapter.deliver(
        NotificationEvent("decision_found", "run_1", None, "strategy", 1, {"x": "y"})
    )

    assert receipt.outcome == "SUCCESS"
    assert receipt.attempts == 2
    assert sleeps == [1]
    assert receipt.batch_id is not None
    assert "example.invalid" not in repr(receipt)
    assert json.loads(requests[-1].data.decode())["event_type"] == "decision_found"


def test_generic_webhook_invalid_secret_never_echoes_its_value() -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("run_started",),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )
    try:
        GenericWebhookNotifier("alpha", destination, environ={"HOOK": "not-a-url-secret"})
    except ValueError as exc:
        assert "not-a-url-secret" not in str(exc)
    else:
        raise AssertionError("invalid secret was accepted")


@pytest.mark.parametrize(
    "line_ending", [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf")]
)
def test_generic_webhook_accepts_one_terminal_line_ending_from_file(
    monkeypatch: pytest.MonkeyPatch, line_ending: str
) -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.application.ports import NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    endpoint = "https://example.invalid/path?token=hidden"
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: endpoint + line_ending)
    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("run_started",),
        SecretRef("file", "/run/secrets/webhook"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )
    requested_urls: list[str] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    def opened(request: Any, *, timeout: float) -> Response:
        requested_urls.append(request.full_url)
        assert timeout == 1
        return Response()

    receipt = GenericWebhookNotifier("alpha", destination, opener=opened).deliver(
        NotificationEvent("run_started", "run", None, "strategy", 1, {})
    )

    assert receipt.outcome == "SUCCESS"
    assert requested_urls == [endpoint]


@pytest.mark.parametrize(
    "secret",
    [
        " https://example.invalid/path",
        "https://example.invalid/path ",
        "https://example.invalid/path\t",
        "https://example.invalid/path\n\n",
        "https://example.invalid/path\r\n\r\n",
        "https://example.invalid/path\r",
        "https://example.invalid/\npath",
        "https://example.invalid/\rpath",
        "https://example.invalid/path\x00",
        "https://example.invalid/path\x85",
        "\n",
    ],
)
def test_generic_webhook_rejects_other_file_secret_whitespace_and_controls(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: secret)
    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("run_started",),
        SecretRef("file", "/run/secrets/webhook"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )

    with pytest.raises(ValueError, match="notification endpoint") as caught:
        GenericWebhookNotifier("alpha", destination)

    assert secret not in str(caught.value)


def test_generic_webhook_does_not_retry_permanent_http_400() -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.application.ports import NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("run_failed",),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(2, (1,)),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(2, 1),
        RedactionConfig((), ()),
        "warning",
    )
    attempts: list[int] = []

    def rejected(*_args: object, **_kwargs: object) -> object:
        attempts.append(1)
        raise HTTPError("https://example.invalid", 400, "bad request", None, None)

    receipt = GenericWebhookNotifier(
        "alpha", destination, environ={"HOOK": "https://example.invalid"}, opener=rejected
    ).deliver(NotificationEvent("run_failed", "run", None, "strategy", 1, {}))

    assert receipt.reason_code == "HTTP_400"
    assert receipt.attempts == 1
    assert attempts == [1]


def test_generic_webhook_batch_posts_ordered_events_once() -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.application.ports import NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found", "run_succeeded"),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(2, 10),
        RedactionConfig((), ()),
        "warning",
    )
    bodies: list[dict[str, object]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 204

    def opened(request: object, *, timeout: float) -> Response:
        assert timeout == 1
        bodies.append(json.loads(request.data.decode()))
        return Response()

    adapter = GenericWebhookNotifier(
        "alpha",
        destination,
        environ={"HOOK": "https://example.invalid"},
        opener=opened,
        clock_seconds=lambda: 20,
    )
    receipt = adapter.deliver_batch(
        (
            NotificationEvent("decision_found", "run", "BTC-USDT-PERP", "strategy", 1, {}),
            NotificationEvent("run_succeeded", "run", None, "strategy", 1, {}),
        )
    )

    assert receipt.outcome == "SUCCESS"
    assert len(bodies) == 1
    assert [event["event_type"] for event in bodies[0]["events"]] == [
        "decision_found",
        "run_succeeded",
    ]


@pytest.mark.parametrize(
    "unsafe_endpoint",
    [
        "https://@endpoint.invalid/path",
        " https://endpoint.invalid/path",
        "https://endpoint.invalid/path\n",
    ],
)
def test_generic_webhook_rejects_empty_userinfo_and_whitespace_without_echoing_secret(
    unsafe_endpoint: str,
) -> None:
    from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("run_started",),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )

    with pytest.raises(ValueError) as caught:
        GenericWebhookNotifier("alpha", destination, environ={"HOOK": unsafe_endpoint})

    assert unsafe_endpoint not in str(caught.value)
