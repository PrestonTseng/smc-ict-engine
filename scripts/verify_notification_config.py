"""Run approved notification rejection vectors at their real contract boundaries."""

from __future__ import annotations

from pathlib import Path

from smc_ict.adapters.notifications import DiscordWebhookNotifier
from smc_ict.configuration import StrictConfigurationError, load_notifications_text
from smc_ict.configuration.models import NotificationDestination, SecretRef

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "config" / "notifications.yaml"


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) < 1:
        raise AssertionError(f"fixture fragment not found: {old!r}")
    return text.replace(old, new, 1)


def must_reject_loader(text: str) -> None:
    try:
        load_notifications_text(text)
    except StrictConfigurationError:
        return
    raise AssertionError("real notification loader accepted an invalid vector")


def must_reject_adapter(unsafe_endpoint: str, destination: NotificationDestination) -> None:
    try:
        DiscordWebhookNotifier(
            "discord_debug",
            destination.model_copy(update={"endpoint": SecretRef("env", "DISCORD_TEST_ENDPOINT")}),
            environ={"DISCORD_TEST_ENDPOINT": unsafe_endpoint},
        )
    except ValueError as exc:
        if unsafe_endpoint:
            assert unsafe_endpoint not in str(exc)
        return
    raise AssertionError("real notification adapter accepted an unsafe endpoint")


def main() -> None:
    source = EXAMPLE.read_text(encoding="utf-8")
    accepted = load_notifications_text(source)
    assert tuple(accepted.destinations) == ("discord_debug",)
    destination = accepted.destinations["discord_debug"]
    assert destination.adapter == "discord_webhook"

    vectors = [
        replace_once(source, "    discord_debug:\n", "    Discord-Debug:\n"),
        replace_once(
            source,
            "      adapter: discord_webhook\n",
            "      adapter: discord_webhook\n      extra: 1\n",
        ),
        replace_once(source, "adapter: discord_webhook", "adapter: unknown"),
        replace_once(
            source,
            "enabled_events: [run_started, run_succeeded, run_failed, decision_found, no_decision]",
            "enabled_events: [decision_found, decision_found]",
        ),
        replace_once(
            source,
            "enabled_events: [run_started, run_succeeded, run_failed, decision_found, no_decision]",
            "enabled_events: []",
        ),
        replace_once(
            source,
            "endpoint:\n        file: /run/secrets/discord_webhook_url",
            "endpoint: {env: DISCORD_TEST_ENDPOINT, file: /run/secrets/shared}",
        ),
        replace_once(
            source,
            "endpoint:\n        file: /run/secrets/discord_webhook_url",
            "endpoint: {url: literal-forbidden}",
        ),
        replace_once(source, "timeout_seconds: 5", "timeout_seconds: true"),
        replace_once(source, "window_seconds: 300", "window_seconds: 0"),
        replace_once(source, "window_seconds: 300", "window_seconds: 86401"),
        replace_once(source, "window_seconds: 300", "window_seconds: true"),
        replace_once(source, "window_seconds: 300", 'window_seconds: "300"'),
        "notifications: {enabled: true, destinations: {}}\n",
        replace_once(
            source,
            "enabled_events: [run_started, run_succeeded, run_failed, decision_found, no_decision]",
            "enabled_events: [unknown_event]",
        ),
        replace_once(
            source,
            "retries:\n        maximum_attempts: 3\n        backoff_seconds: [1, 2]",
            "retries: wrong",
        ),
    ]
    for vector in vectors:
        must_reject_loader(vector)
    for unsafe in (
        "",
        "http://endpoint.invalid/x",
        "https://user@endpoint.invalid/x",
        "https://endpoint.invalid/x#part",
    ):
        must_reject_adapter(unsafe, destination)

    for boundary in (1, 86_400):
        bounded = replace_once(source, "window_seconds: 300", f"window_seconds: {boundary}")
        load_notifications_text(bounded)

    print("PASS real loader notification example: destinations=1 adapter=discord_webhook")
    print("PASS structural loader and adapter-boundary rejection vectors: 18")
    print("PASS real loader deduplication boundaries: 1, 86400")
    print("PASS no resolved endpoint retained in typed model or redacted hash input")


if __name__ == "__main__":
    main()
