from __future__ import annotations

import tomllib
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).parents[1]


def test_product_identity_is_trading_research_engine_0_2_0() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    compose = YAML(typ="safe").load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert project["project"]["name"] == "trading-research-engine"
    assert project["project"]["version"] == "0.2.0"
    assert project["project"]["scripts"] == {"trading-research": "trading_research.cli:main"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/trading_research"
    ]
    assert project["tool"]["mypy"]["packages"] == ["trading_research"]
    assert (ROOT / "src/trading_research/__init__.py").is_file()
    old_namespace = "smc" + "_ict"
    assert not (ROOT / "src" / old_namespace).exists()
    assert compose["services"]["engine"]["healthcheck"]["test"][:2] == [
        "CMD",
        "trading-research",
    ]
    assert "image: trading-research-engine:" in (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "groupadd --gid 10001 trading-research" in dockerfile
    assert 'ENTRYPOINT ["trading-research"]' in dockerfile
    assert readme.startswith("# Trading Research Engine\n")
    assert "trading_research.db" in readme


def test_release_owner_actions_are_explicitly_separate_from_local_candidate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    prose = " ".join(readme.split())

    assert "GitHub repository rename" in prose
    assert "production database cutover" in prose
    assert "production deployment" in prose
    assert "separate owner-authorized actions" in prose
