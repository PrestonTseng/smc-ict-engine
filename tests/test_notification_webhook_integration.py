from __future__ import annotations

import json
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from smc_ict.application.notifications import NotificationRouter
from smc_ict.application.ports import NotificationEvent
from smc_ict.configuration.models import (
    BatchingConfig,
    DeduplicationConfig,
    NotificationConfig,
    NotificationDestination,
    RedactionConfig,
    RetryConfig,
    SecretRef,
    frozen_mapping,
)


def test_generic_webhooks_use_real_local_tls_and_isolate_a_failed_destination(
    tmp_path: Path, monkeypatch: object
) -> None:
    certificate = tmp_path / "certificate.pem"
    private_key = tmp_path / "private-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))  # type: ignore[attr-defined]

    deliveries: list[tuple[str, dict[str, object]]] = []
    attempts: dict[str, int] = {"/good": 0, "/flaky": 0, "/bad": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            attempts[self.path] += 1
            length = int(self.headers["Content-Length"])
            deliveries.append((self.path, json.loads(self.rfile.read(length))))
            status = (
                500
                if self.path == "/bad" or (self.path == "/flaky" and attempts[self.path] == 1)
                else 204
            )
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def destination(endpoint_name: str, attempts_count: int) -> NotificationDestination:
        return NotificationDestination(
            "generic_webhook",
            True,
            ("decision_found",),
            SecretRef("env", endpoint_name),
            2,
            RetryConfig(attempts_count, (0,) * (attempts_count - 1)),
            DeduplicationConfig(60, ("event_type", "run_id", "instrument_id")),
            BatchingConfig(10, 1),
            RedactionConfig(("authorization",), ("token",)),
            "warning",
        )

    monkeypatch.setenv("GOOD", f"https://localhost:{port}/good")  # type: ignore[attr-defined]
    monkeypatch.setenv("FLAKY", f"https://localhost:{port}/flaky")  # type: ignore[attr-defined]
    monkeypatch.setenv("BAD", f"https://localhost:{port}/bad")  # type: ignore[attr-defined]
    config = NotificationConfig(
        True,
        frozen_mapping(
            {
                "bad": destination("BAD", 1),
                "flaky": destination("FLAKY", 2),
                "good": destination("GOOD", 1),
            }
        ),
    )
    try:
        from smc_ict.adapters.notifications.generic_webhook import GenericWebhookNotifier

        router = NotificationRouter(
            config,
            adapter_factory=lambda destination_id, item: GenericWebhookNotifier(
                destination_id, item, sleeper=lambda _seconds: None, clock_seconds=lambda: 1
            ),
            clock_seconds=lambda: 1,
        )
        receipt = router.deliver(
            NotificationEvent(
                "decision_found", "run-1", "BTC-USDT-PERP", "fixture", 1, {"state": "ready"}
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert receipt.outcome == "PARTIAL_FAILURE"
    assert [(item.destination_id, item.outcome, item.attempts) for item in receipt.receipts] == [
        ("bad", "FAILURE", 1),
        ("flaky", "SUCCESS", 2),
        ("good", "SUCCESS", 1),
    ]
    assert attempts == {"/good": 1, "/flaky": 2, "/bad": 1}
    assert all(payload["event_type"] == "decision_found" for _, payload in deliveries)
    assert all(payload["instrument_id"] == "BTC-USDT-PERP" for _, payload in deliveries)
    assert "localhost" not in repr(receipt)
