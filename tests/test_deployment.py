from __future__ import annotations

import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

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


def test_packages_exclude_deterministic_fictional_runtime_and_secret_material(
    tmp_path: Path,
) -> None:
    project = tmp_path / "fictional-project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "README.md").write_text("fictional package-boundary probe\n", encoding="utf-8")
    package = project / "src" / "smc_ict"
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
        "src/smc_ict/__pycache__/probe.pyc",
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

    assert service["command"] == [
        "scheduler",
        "--schedule",
        "/config/schedule.yaml",
        "--database",
        "/data/smc_ict.db",
        "--lock",
        "/data/engine.lock",
        "--health-file",
        "/data/scheduler.ready",
        "--config-root",
        "/",
    ]
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
    ]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "smc-ict",
        "scheduler-health",
        "--health-file",
        "/data/scheduler.ready",
    ]


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
    ]
    assert engine["command"][0:2] == ["scheduler", "--schedule"]
    assert engine["command"][-2:] == ["--config-root", "/"]

    host_fixture = Path("/Users/preston/Repository/agents/tools/smc-ict-engine")
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
    host_fixture = Path("/Users/preston/Repository/agents/tools/smc-ict-engine")
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


def test_data_folder_preflight_accepts_only_real_absolute_directories(tmp_path: Path) -> None:
    script = ROOT / "scripts/preflight-data-folder.sh"
    assert script.is_file()
    valid = tmp_path / "state with spaces"
    valid.mkdir()
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("fixture\n", encoding="utf-8")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "state").mkdir()
    symlink_leaf = tmp_path / "state-link"
    symlink_leaf.symlink_to(valid, target_is_directory=True)
    symlink_parent = tmp_path / "parent-link"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    accepted = subprocess.run(
        [str(script)],
        env={**os.environ, "DATA_FOLDER": str(valid)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr

    rejected = (
        None,
        "",
        "relative/data",
        str(tmp_path / "missing"),
        str(regular_file),
        str(symlink_leaf),
        str(symlink_parent / "state"),
    )
    for data_folder in rejected:
        environment = os.environ.copy()
        if data_folder is None:
            environment.pop("DATA_FOLDER", None)
        else:
            environment["DATA_FOLDER"] = data_folder
        result = subprocess.run(
            [str(script)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, data_folder
        assert "DATA_FOLDER" in result.stderr


def test_compose_operator_entry_runs_data_folder_preflight_before_compose() -> None:
    wrapper_path = ROOT / "scripts/compose.sh"
    assert wrapper_path.is_file()
    wrapper = wrapper_path.read_text(encoding="utf-8")

    assert '"$SCRIPT_DIR/preflight-data-folder.sh"' in wrapper
    assert 'exec docker compose "$@"' in wrapper


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
        "src/smc_ict/application/runtime.py",
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
        "src/smc_ict/__pycache__/fictional.pyc",
        "src/smc_ict/.cache/fictional.json",
        "src/smc_ict/nested/runtime.sqlite3",
        "src/smc_ict/nested/source.pine",
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
        '"$DATA_FOLDER/smc_ict.db"',
        "/data/smc_ict.db",
        "./scripts/compose.sh up -d --build",
        'sqlite3 "$DATA_FOLDER/smc_ict.db"',
        "smc-ict notifier-test",
    ):
        assert section in readme

    assert "The Compose health command reads the scheduler readiness marker" in readme
    assert "The Compose health command reads `/data/smc_ict.db`" not in readme
    assert "--config-root config" in readme
    assert "--config-root ." not in readme
    assert "./data" not in readme


def test_operator_docs_cover_the_single_secret_release_workflow_and_runtime_evidence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    operator_docs = "\n".join((readme, operations, configuration))

    for required_command in (
        'install -d -m 0750 -o 10001 -g 10001 "$DATA_FOLDER"',
        "secrets/discord_webhook_url",
        'export SMC_ICT_GIT_COMMIT="$(git rev-parse HEAD)"',
        "./scripts/compose.sh build engine",
        "./scripts/compose.sh up -d engine",
        "./scripts/compose.sh ps",
        "./scripts/compose.sh logs --tail 100 engine",
        'smc-ict database status --database "$DATA_FOLDER/smc_ict.db"',
        "./scripts/compose.sh --profile manual run --rm manual run",
        "./scripts/compose.sh down",
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
    assert "./scripts/compose.sh ps" in troubleshooting
    assert "lsof ./data/engine.lock" not in troubleshooting
    assert "docker compose ps" not in troubleshooting


def test_required_operator_document_set_is_present_and_cross_linked() -> None:
    required = {
        "concepts.md": "research-only",
        "strategy-authoring.md": "seven implemented registrations",
        "configuration.md": "config/notifications.yaml",
        "operations.md": "./scripts/compose.sh stop --timeout 30 engine",
        "troubleshooting.md": "PROCESS_RESTART",
        "architecture.md": "five tables",
        "formula-provenance.md": "active source locators",
    }
    for filename, boundary in required.items():
        document = ROOT.joinpath("docs", filename).read_text(encoding="utf-8")
        assert boundary in document

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert env_example == "SMC_ICT_GIT_COMMIT=\nDATA_FOLDER=/absolute/path/to/smc-ict-data\n"


def test_schema_uses_json_validation_supported_by_the_container_sqlite() -> None:
    from smc_ict.adapters.persistence.sqlite import DDL

    assert "json_valid(source_fields_json)" in DDL
    assert "json_valid(payload_json)" in DDL
    assert "json_valid(source_fields_json," not in DDL
    assert "json_valid(payload_json," not in DDL
