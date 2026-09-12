from __future__ import annotations

import json
import logging
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).parents[1]
UV_0_11_6_INDEX_DIGEST = "sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754"
PROTECTED_MOUNT_PROBE = """set -eu;
probe=/data/.mount-write-probe;
: > "$probe";
test -f "$probe";
rm "$probe";
test ! -e "$probe";
if touch /config/.mount-write-probe; then
    rm -f /config/.mount-write-probe;
    exit 1;
fi;
if touch /strategies/.mount-write-probe; then
    rm -f /strategies/.mount-write-probe;
    exit 1;
fi;
if touch /run/secrets/discord_webhook_url; then
    exit 1;
fi;
echo PROBE_OK
"""


def _run_cli(argv: list[str]) -> int:
    from trading_research.cli import main

    logger = logging.getLogger("trading_research")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    try:
        return main(argv)
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _daemon_visible_project_fixture_candidates() -> tuple[Path, ...]:
    legacy_host_directory = "smc" + "-ict-engine"
    return (
        ROOT,
        Path("/Users/preston/Repository/agents/tools") / legacy_host_directory,
    )


def _daemon_visible_project_fixture(candidates: tuple[Path, ...] | None = None) -> Path:
    failures: list[str] = []
    for candidate in candidates or _daemon_visible_project_fixture_candidates():
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                f"type=bind,source={candidate},target=/fixture,readonly",
                "busybox:1.37.0",
                "sh",
                "-c",
                (
                    "test -d /fixture/config && "
                    "test -d /fixture/strategies && "
                    "test -f /fixture/README.md"
                ),
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
        failures.append(f"{candidate}: exit {probe.returncode}: {probe.stderr.strip()}")
    raise AssertionError("no daemon-visible project fixture found:\n" + "\n".join(failures))


def test_daemon_visible_project_fixture_survives_a_missing_host_candidate() -> None:
    missing_candidate = Path("/daemon-host/path-that-must-not-exist/trading-research-engine")

    fixture = _daemon_visible_project_fixture(
        (missing_candidate, *_daemon_visible_project_fixture_candidates())
    )

    assert fixture != missing_candidate
    probe = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={fixture},target=/fixture,readonly",
            "busybox:1.37.0",
            "sh",
            "-c",
            "test -d /fixture/config && test -d /fixture/strategies && test -f /fixture/README.md",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_packages_exclude_deterministic_fictional_runtime_and_secret_material(
    tmp_path: Path,
) -> None:
    project = tmp_path / "fictional-project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "README.md").write_text("fictional package-boundary probe\n", encoding="utf-8")
    package = project / "src" / "trading_research"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("PROBE = True\n", encoding="utf-8")

    fictional_members = (
        ".env",
        ".env.probe",
        ".mypy_cache/probe.json",
        ".pytest_cache/probe",
        ".ruff_cache/probe",
        "data/probe.sqlite",
        "reports/probe.json",
        "secrets/fictional_webhook_url",
        "src/trading_research/__pycache__/probe.pyc",
    )
    for relative in fictional_members:
        fixture = project / relative
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("fictional-not-a-secret\n", encoding="utf-8")

    result = subprocess.run(
        ["uv", "build", "--sdist", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sdist = next((tmp_path / "dist").glob("*.tar.gz"))
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_members = {"/".join(Path(member.name).parts[1:]) for member in archive}
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = set(archive.namelist())

    for fictional_member in fictional_members:
        assert fictional_member not in sdist_members
        wheel_member = fictional_member.removeprefix("src/")
        assert wheel_member not in wheel_members


def test_compose_contract_keeps_the_database_in_the_required_bind_mount() -> None:
    compose_path = ROOT / "compose.yaml"
    assert compose_path.is_file()

    compose = YAML(typ="safe").load(compose_path.read_text(encoding="utf-8"))
    service = compose["services"]["engine"]

    assert service["environment"] == {"CONFIG_FOLDER": "/config", "DATA_FOLDER": "/data"}
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": "${DATA_FOLDER:?Set DATA_FOLDER to the writable host data directory}",
            "target": "/data",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./config",
            "target": "/config",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./strategies",
            "target": "/strategies",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./backtests",
            "target": "/backtests",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
    ]


def test_compose_engine_command_executes_with_runtime_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trading_research import cli

    engine = YAML(typ="safe").load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"][
        "engine"
    ]
    data_folder = tmp_path / "data"
    config_folder = tmp_path / "config"
    data_folder.mkdir()
    config_folder.mkdir()
    monkeypatch.setenv("DATA_FOLDER", str(data_folder))
    monkeypatch.setenv("CONFIG_FOLDER", str(config_folder))
    captured: dict[str, object] = {}

    class SchedulerProbe:
        def start(self) -> None:
            captured["started"] = True

        def health(self) -> SimpleNamespace:
            return SimpleNamespace(configured_jobs=0, recovered_run_ids=())

        def shutdown(self, *, wait: bool) -> None:
            captured["shutdown_wait"] = wait

    class NonBlockingEvent:
        def wait(self) -> None:
            captured["waited"] = True

    def build_scheduler_probe(**kwargs: object) -> SchedulerProbe:
        captured.update(kwargs)
        return SchedulerProbe()

    monkeypatch.setattr(cli, "build_scheduler", build_scheduler_probe)
    monkeypatch.setattr(cli, "Event", NonBlockingEvent)

    assert _run_cli(engine["command"]) == 0
    assert captured == {
        "schedule_path": "/config/schedule.yaml",
        "database": data_folder / "trading_research.db",
        "lock_path": data_folder / "engine.lock",
        "config_root": config_folder,
        "started": True,
        "waited": True,
        "shutdown_wait": True,
    }
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == [
        "READY",
        "SHUTDOWN",
    ]
    assert not (data_folder / "scheduler.ready").exists()


def test_compose_healthcheck_executes_against_data_folder_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = YAML(typ="safe").load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"][
        "engine"
    ]
    data_folder = tmp_path / "data"
    data_folder.mkdir()
    monkeypatch.setenv("DATA_FOLDER", str(data_folder))
    (data_folder / "scheduler.ready").write_text(
        json.dumps({"pid": os.getpid(), "status": "READY"}), encoding="utf-8"
    )
    command = engine["healthcheck"]["test"]
    assert command[:2] == ["CMD", "trading-research"]

    assert _run_cli(command[2:]) == 0
    assert json.loads(capsys.readouterr().out) == {"pid": os.getpid(), "status": "READY"}


def test_compose_avoids_the_legacy_nested_read_only_bind_mount_and_starts_a_fresh_probe(
    tmp_path: Path,
) -> None:
    compose = YAML(typ="safe").load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    engine = compose["services"]["engine"]

    assert engine["volumes"] == [
        {
            "type": "bind",
            "source": "${DATA_FOLDER:?Set DATA_FOLDER to the writable host data directory}",
            "target": "/data",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./config",
            "target": "/config",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./strategies",
            "target": "/strategies",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "./backtests",
            "target": "/backtests",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
    ]
    host_fixture = _daemon_visible_project_fixture()
    host_config = host_fixture / "config"
    host_strategies = host_fixture / "strategies"
    host_secret_fixture = host_fixture / "README.md"
    legacy = tmp_path / "legacy-compose.yaml"
    legacy.write_text(
        f"""services:
  probe:
    image: busybox:1.37.0
    read_only: true
    command: [\"sh\", \"-c\", \"true\"]
    volumes:
      - type: bind
        source: {host_strategies}
        target: /config
        read_only: true
        bind: {{create_host_path: false}}
      - type: bind
        source: {host_config}
        target: /config/strategies
        read_only: true
        bind: {{create_host_path: false}}
""",
        encoding="utf-8",
    )
    legacy_project = f"legacy-mount-contract-{os.getpid()}"
    repaired = tmp_path / "repaired-compose.yaml"
    repaired.write_text(
        f"""services:
  probe:
    image: busybox:1.37.0
    user: \"10001:10001\"
    read_only: true
    cap_drop: [ALL]
    security_opt: [\"no-new-privileges:true\"]
    tmpfs: [\"/tmp:rw,noexec,nosuid,size=16m\"]
    command: ["sh", "-c", {json.dumps(PROTECTED_MOUNT_PROBE.replace("$", "$$"))}]
    volumes:
      - type: volume
        source: probe_data
        target: /data
      - type: bind
        source: {host_config}
        target: /config
        read_only: true
        bind: {{create_host_path: false}}
      - type: bind
        source: {host_strategies}
        target: /strategies
        read_only: true
        bind: {{create_host_path: false}}
    secrets:
      - discord_webhook_url
secrets:
  discord_webhook_url:
    file: {host_secret_fixture}
volumes:
  probe_data:
""",
        encoding="utf-8",
    )
    repaired_project = f"repaired-mount-contract-{os.getpid()}"
    ownership_container = f"{repaired_project}-data-owner"
    try:
        legacy_start = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                legacy_project,
                "-f",
                str(legacy),
                "up",
                "--abort-on-container-exit",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert legacy_start.returncode != 0
        assert "create mountpoint for /config/strategies mount" in legacy_start.stderr
        assert "read-only file system" in legacy_start.stderr

        repaired_create = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                repaired_project,
                "-f",
                str(repaired),
                "create",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert repaired_create.returncode == 0, repaired_create.stderr
        container_id = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                repaired_project,
                "-f",
                str(repaired),
                "ps",
                "--all",
                "-q",
                "probe",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert container_id.returncode == 0, container_id.stderr
        assert container_id.stdout.strip()
        inspection = subprocess.run(
            ["docker", "inspect", container_id.stdout.strip()],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert inspection.returncode == 0, inspection.stderr
        container = json.loads(inspection.stdout)[0]
        mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
        assert [mount["Destination"] for mount in container["Mounts"] if mount["RW"]] == ["/data"]
        assert mounts["/data"]["Type"] == "volume"
        initialize_data = subprocess.run(
            [
                "docker",
                "run",
                "--name",
                ownership_container,
                "--user",
                "0:0",
                "--volume",
                f"{mounts['/data']['Name']}:/data",
                "busybox:1.37.0",
                "chown",
                "10001:10001",
                "/data",
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert initialize_data.returncode == 0, initialize_data.stderr
        assert mounts["/config"]["RW"] is False
        assert mounts["/strategies"]["RW"] is False
        assert mounts["/run/secrets/discord_webhook_url"]["RW"] is False
        assert container["Config"]["User"] == "10001:10001"
        assert container["HostConfig"]["ReadonlyRootfs"] is True
        assert container["HostConfig"]["CapDrop"] == ["ALL"]
        assert container["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
        assert container["HostConfig"]["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=16m"}
        repaired_start = subprocess.run(
            ["docker", "start", "-a", container_id.stdout.strip()],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert repaired_start.returncode == 0, repaired_start.stderr
        assert repaired_start.stdout.strip().endswith("PROBE_OK")
    finally:
        subprocess.run(
            ["docker", "rm", "--force", ownership_container],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                repaired_project,
                "-f",
                str(repaired),
                "down",
                "--volumes",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                legacy_project,
                "-f",
                str(legacy),
                "down",
                "--volumes",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        for project in (legacy_project, repaired_project):
            for resource_command in (
                ["docker", "ps", "--all", "--quiet"],
                ["docker", "network", "ls", "--quiet"],
                ["docker", "volume", "ls", "--quiet"],
            ):
                remaining = subprocess.run(
                    [
                        *resource_command,
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                assert remaining.returncode == 0, remaining.stderr
                assert remaining.stdout.strip() == ""


@pytest.mark.parametrize(
    "writable_target",
    (None, "/config", "/strategies", "/run/secrets/discord_webhook_url"),
    ids=("hardened", "writable-config", "writable-strategies", "writable-secret"),
)
def test_protected_mount_probe_rejects_each_writable_target(
    writable_target: str | None,
) -> None:
    host_fixture = _daemon_visible_project_fixture()
    protected_mounts = {
        "/config": host_fixture / "config",
        "/strategies": host_fixture / "strategies",
        "/run/secrets/discord_webhook_url": host_fixture / "README.md",
    }
    container_name = (
        f"protected-mount-probe-{os.getpid()}-"
        f"{(writable_target or 'hardened').replace('/', '-').strip('-')}"
    )
    command = [
        "docker",
        "run",
        "--name",
        container_name,
        "--rm",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--tmpfs",
        "/data:rw,noexec,nosuid,mode=1777,size=16m",
    ]
    for target, source in protected_mounts.items():
        if target == writable_target:
            tmpfs_target = "/run/secrets" if target.startswith("/run/secrets/") else target
            command.extend(("--tmpfs", f"{tmpfs_target}:rw,noexec,nosuid,mode=1777,size=16m"))
        else:
            command.extend(
                (
                    "--mount",
                    f"type=bind,source={source},target={target},readonly",
                )
            )
    command.extend(("busybox:1.37.0", "sh", "-c", PROTECTED_MOUNT_PROBE))

    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if writable_target is None:
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip().endswith("PROBE_OK")
        else:
            assert result.returncode == 1, (
                f"probe did not reject writable target {writable_target}: "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
            assert "PROBE_OK" not in result.stdout
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )


def test_compose_operator_contract_uses_direct_commands_and_native_guards() -> None:
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    operator_docs = "\n".join(
        (ROOT / filename).read_text(encoding="utf-8")
        for filename in ("README.md", "docs/operations.md", "docs/troubleshooting.md")
    )

    assert not (ROOT / "scripts/compose.sh").exists()
    assert not (ROOT / "scripts/preflight-data-folder.sh").exists()
    assert "./scripts/compose.sh" not in operator_docs
    assert "scripts/preflight-data-folder.sh" not in operator_docs
    assert "docker compose config --quiet" in operator_docs
    assert "docker compose up -d engine" in operator_docs
    assert "docker compose ps" in operator_docs
    assert "${DATA_FOLDER:?Set DATA_FOLDER to the writable host data directory}" in compose_text
    assert compose_text.count("create_host_path: false") == 4


def test_compose_and_image_apply_non_root_immutable_runtime_hardening() -> None:
    compose = YAML(typ="safe").load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    engine = compose["services"]["engine"]

    assert engine["user"] == "10001:10001"
    assert engine["read_only"] is True
    assert engine["cap_drop"] == ["ALL"]
    assert engine["security_opt"] == ["no-new-privileges:true"]
    assert engine["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]
    assert engine["restart"] == "unless-stopped"
    assert engine["pull_policy"] == "never"
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "0000000000000000000000000000000000000000" not in compose_text
    manual = compose["services"]["manual"]
    assert manual["profiles"] == ["manual"]
    assert manual["user"] == "10001:10001"
    assert manual["environment"] == {"CONFIG_FOLDER": "/config", "DATA_FOLDER": "/data"}
    assert "secrets" not in manual

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "FROM python:3.13.6-slim-trixie"
        "@sha256:2a928e11761872b12003515ea59b3c40bb5340e2e5ecc1108e043f92be7e473d" in dockerfile
    )
    assert "FROM python:3.13.6-slim-trixie\n" not in dockerfile
    assert "COPY uv.lock" in dockerfile
    assert "uv sync --locked" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "ARG GIT_COMMIT=" not in dockerfile
    assert "grep -Eq '^[0-9a-f]{40}$'" in dockerfile
    assert "0000000000000000000000000000000000000000" in dockerfile


def test_dockerfile_executes_uv_from_the_verified_immutable_0_11_6_index() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.splitlines()[0] == (
        f"FROM ghcr.io/astral-sh/uv:0.11.6@{UV_0_11_6_INDEX_DIGEST} AS uv"
    )
    assert dockerfile.count("ghcr.io/astral-sh/uv") == 1
    assert "COPY --from=uv /uv /usr/local/bin/uv" in dockerfile
    assert "RUN uv sync --locked --no-dev --no-editable" in dockerfile


def test_docker_build_context_contains_required_inputs_and_excludes_private_artifacts(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    required_files = (
        "Dockerfile",
        "uv.lock",
        "pyproject.toml",
        "README.md",
        "src/trading_research/application/runtime.py",
    )
    for relative in required_files:
        source = ROOT / relative
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (context / ".dockerignore").write_bytes((ROOT / ".dockerignore").read_bytes())

    forbidden_files = (
        ".env.probe",
        "nested/.env.local",
        "reports/fictional-review.md",
        "nested/runtime.db",
        "nested/runtime.db-wal",
        "nested/runtime.sqlite",
        "nested/runtime.sqlite3-shm",
        "nested/source.pine",
        "nested/source.pine.txt",
        "nested/fictional-private-key.pem",
        "nested/fictional.secret",
        "secrets/fictional-webhook",
        "data/fictional-runtime.json",
        ".git/fictional-config",
        "src/trading_research/__pycache__/fictional.pyc",
        "src/trading_research/.cache/fictional.json",
        "src/trading_research/nested/runtime.sqlite3",
        "src/trading_research/nested/source.pine",
    )
    for relative in forbidden_files:
        fixture = context / relative
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("fictional-not-a-secret\n", encoding="utf-8")

    probe_dockerfile = tmp_path / "Dockerfile.context-probe"
    probe_dockerfile.write_text("FROM scratch\nCOPY . /context/\n", encoding="utf-8")
    build = subprocess.run(
        [
            "docker",
            "build",
            "--no-cache",
            "--file",
            str(probe_dockerfile),
            "--quiet",
            str(context),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    image_id = build.stdout.strip().splitlines()[-1]
    container_id = ""
    try:
        create = subprocess.run(
            ["docker", "create", image_id, "context-probe"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert create.returncode == 0, create.stderr
        container_id = create.stdout.strip()
        archive_path = tmp_path / "context.tar"
        export = subprocess.run(
            ["docker", "export", "--output", str(archive_path), container_id],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert export.returncode == 0, export.stderr
        with tarfile.open(archive_path) as archive:
            members = {member.name.removeprefix("context/") for member in archive}
        assert set(required_files) <= members
        assert set(forbidden_files).isdisjoint(members)
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )
        subprocess.run(
            ["docker", "image", "rm", "--force", image_id],
            capture_output=True,
            timeout=30,
            check=False,
        )


def test_readme_documents_the_operator_workflows_and_safety_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for section in (
        "## Docker Compose operation",
        "## Configuration",
        "## Strategy DAG authoring",
        "## Notification destinations",
        "## Recovery and backup",
        "## Financial-risk boundary",
        '"$DATA_FOLDER/trading_research.db"',
        "/data/trading_research.db",
        "docker compose up -d --build",
        'sqlite3 "$DATA_FOLDER/trading_research.db"',
        "trading-research notifier-test",
    ):
        assert section in readme

    assert "The Compose health command reads the scheduler readiness marker" in readme
    assert "The Compose health command reads `/data/trading_research.db`" not in readme
    assert 'export DATA_FOLDER="$(pwd)/data"' in readme
    assert 'export CONFIG_FOLDER="$(pwd)/config"' in readme
    assert "./data" not in readme


def test_documented_operator_commands_parse_with_global_runtime_authority() -> None:
    from trading_research.cli import _parser

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    operator_docs = "\n".join((readme, operations))

    forbidden_operation_arguments = (
        "database bootstrap --database",
        "database status --database",
        "--database /data/trading_research.db",
        "--lock /data/engine.lock",
        "--config-root",
        "--health-file",
        "--market-data /config/market-data.yaml",
    )
    for forbidden_argument in forbidden_operation_arguments:
        assert forbidden_argument not in operator_docs

    expected_shapes = (
        (
            ["database", "bootstrap"],
            {"command": "database", "database_command": "bootstrap"},
        ),
        (["database", "status"], {"command": "database", "database_command": "status"}),
        (
            [
                "run",
                "--strategy",
                "strategies/source-aligned-research.yaml",
                "--notifications",
                "config/notifications.yaml",
                "--trigger",
                "manual",
            ],
            {
                "command": "run",
                "strategy": "strategies/source-aligned-research.yaml",
                "notifications": "config/notifications.yaml",
                "trigger": "manual",
            },
        ),
        (
            ["scheduler", "--schedule", "config/schedule.yaml"],
            {"command": "scheduler", "schedule": "config/schedule.yaml"},
        ),
        (["scheduler-health"], {"command": "scheduler-health"}),
        (
            ["backtest", "backtests/source-aligned-research/one-year-baseline.yaml"],
            {
                "command": "backtest",
                "scenario": "backtests/source-aligned-research/one-year-baseline.yaml",
            },
        ),
        (
            [
                "run",
                "--strategy",
                "/strategies/source-aligned-research.yaml",
                "--trigger",
                "manual",
            ],
            {
                "command": "run",
                "strategy": "/strategies/source-aligned-research.yaml",
                "notifications": None,
                "trigger": "manual",
            },
        ),
        (
            ["backtest", "/backtests/source-aligned-research/one-year-baseline.yaml"],
            {
                "command": "backtest",
                "scenario": "/backtests/source-aligned-research/one-year-baseline.yaml",
            },
        ),
    )
    parser = _parser()
    for argv, expected in expected_shapes:
        assert vars(parser.parse_args(argv)) == expected


def test_documented_database_and_scheduler_health_commands_execute_from_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_folder = tmp_path / "data"
    data_folder.mkdir()
    monkeypatch.setenv("DATA_FOLDER", str(data_folder))

    assert _run_cli(["database", "bootstrap"]) == 0
    assert _run_cli(["database", "status"]) == 0
    (data_folder / "scheduler.ready").write_text(
        json.dumps({"pid": os.getpid(), "status": "READY"}), encoding="utf-8"
    )
    assert _run_cli(["scheduler-health"]) == 0

    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert payloads[0] == {"schema_version": 1, "status": "READY", "tables": 5}
    assert payloads[1]["runs"] == 0
    assert payloads[2] == {"pid": os.getpid(), "status": "READY"}


def test_operator_docs_cover_the_single_secret_release_workflow_and_runtime_evidence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    operator_docs = "\n".join((readme, operations, configuration))

    for required_command in (
        'install -d -m 0750 -o 10001 -g 10001 "$DATA_FOLDER"',
        "secrets/discord_webhook_url",
        'export SMC_ICT_GIT_COMMIT="$(git rev-parse HEAD)"',
        "docker compose build engine",
        "docker compose up -d engine",
        "docker compose ps",
        "docker compose logs --tail 100 engine",
        "trading-research database status",
        "docker compose --profile manual run --rm manual run",
        "docker compose down",
    ):
        assert required_command in operations

    assert "four runs per hour" in operations
    assert "15-minute boundaries" in operations
    assert "provider synchronization" in operations
    assert "Discord delivery" in operations
    assert "DISCORD_1_WEBHOOK_URL" not in operator_docs
    assert "discord_2_webhook_url" not in operator_docs
    assert "https://" not in operator_docs
    assert "./data" not in operations


def test_operator_docs_describe_required_env_and_safe_status_commands() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    troubleshooting = (ROOT / "docs/troubleshooting.md").read_text(encoding="utf-8")

    assert (
        "immutable image revision and required absolute `DATA_FOLDER` configuration"
        in configuration
    )
    assert 'lsof "$DATA_FOLDER/engine.lock"' in troubleshooting
    assert "docker compose ps" in troubleshooting
    assert "lsof ./data/engine.lock" not in troubleshooting


def test_required_operator_document_set_is_present_and_cross_linked() -> None:
    required = {
        "concepts.md": "research-only",
        "strategy-authoring.md": "seven implemented registrations",
        "configuration.md": "config/notifications.yaml",
        "operations.md": "docker compose stop --timeout 30 engine",
        "troubleshooting.md": "PROCESS_RESTART",
        "architecture.md": "five tables",
        "formula-provenance.md": "active source locators",
    }
    for filename, boundary in required.items():
        document = ROOT.joinpath("docs", filename).read_text(encoding="utf-8")
        assert boundary in document

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (
        env_example == "SMC_ICT_GIT_COMMIT=\nDATA_FOLDER=/absolute/path/to/trading-research-data\n"
    )


def test_operator_docs_cover_immutable_backtest_workflow_and_failures() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "trading-research backtest",
        "manifest.json",
        "pipeline-traces.jsonl",
        "report.html",
    ):
        assert phrase in readme

    expectations = {
        "architecture.md": ("SQLite snapshot", "atomic rename", "production tables"),
        "configuration.md": ("backtests/<strategy-id>/", "existing_result", "market-data.yaml"),
        "operations.md": ("--profile manual run --rm manual", "backtest /backtests/"),
        "troubleshooting.md": ("existing backtest result differs", "snapshot candle range"),
    }
    for filename, phrases in expectations.items():
        document = ROOT.joinpath("docs", filename).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in document


def test_human_facing_runtime_paths_and_manual_notifications_match_compose() -> None:
    design = (ROOT / "docs/deployment-design.html").read_text(encoding="utf-8")
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    manual_command = operations.split("## Run one manual receipt path", 1)[1].split("##", 1)[0]

    assert "${DATA_FOLDER}/trading_research.db" in design
    assert "./data/trading_research.db" not in design
    assert "${DATA_FOLDER}/backtests/<backtest-id>/report.html" in operations
    assert "--notifications" not in manual_command
    assert "does not load notification configuration or send\nnotifications" in manual_command


def test_schema_uses_json_validation_supported_by_the_container_sqlite() -> None:
    from trading_research.adapters.persistence.sqlite import DDL

    assert "json_valid(source_fields_json)" in DDL
    assert "json_valid(payload_json)" in DDL
    assert "json_valid(source_fields_json," not in DDL
    assert "json_valid(payload_json," not in DDL
