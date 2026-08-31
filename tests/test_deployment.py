from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).parents[1]
UV_0_11_6_INDEX_DIGEST = "sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754"


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
    ]
    assert "./data:/data" in service["volumes"]
    assert "./config:/config:ro" in service["volumes"]
    assert "./strategies:/config/strategies:ro" in service["volumes"]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "smc-ict",
        "scheduler-health",
        "--health-file",
        "/data/scheduler.ready",
    ]


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
        "./data/smc_ict.db",
        "/data/smc_ict.db",
        "docker compose up -d --build",
        "sqlite3 ./data/smc_ict.db '.backup",
        "smc-ict notifier-test",
    ):
        assert section in readme

    assert "The Compose health command reads the scheduler readiness marker" in readme
    assert "The Compose health command reads `/data/smc_ict.db`" not in readme


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
    assert env_example == "DISCORD_1_WEBHOOK_URL=\nSMC_ICT_GIT_COMMIT=\n"


def test_schema_uses_json_validation_supported_by_the_container_sqlite() -> None:
    from smc_ict.adapters.persistence.sqlite import DDL

    assert "json_valid(source_fields_json)" in DDL
    assert "json_valid(payload_json)" in DDL
    assert "json_valid(source_fields_json," not in DDL
    assert "json_valid(payload_json," not in DDL
