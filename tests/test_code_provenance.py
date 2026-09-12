from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest


def _expected_code_hash(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"trading-research-code-v1\0")
    for path in sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def test_code_hash_is_canonical_and_independent_of_root_location(tmp_path: Path) -> None:
    from trading_research.provenance import calculate_code_hash

    first = tmp_path / "first" / "trading_research"
    first.mkdir(parents=True)
    (first / "__init__.py").write_bytes(b'VERSION = "1"\n')
    (first / "z.py").write_bytes(b"Z = 1\n")
    nested = first / "nested"
    nested.mkdir()
    (nested / "a.py").write_bytes(b"A = 2\n")
    second = tmp_path / "elsewhere" / "package"
    shutil.copytree(first, second)

    first_hash = calculate_code_hash(first)
    second_hash = calculate_code_hash(second)

    assert first_hash == second_hash == _expected_code_hash(first)
    assert len(first_hash) == 64
    assert set(first_hash) <= set("0123456789abcdef")


def test_code_hash_fails_closed_for_unsafe_package_roots_and_members(tmp_path: Path) -> None:
    from trading_research.provenance import calculate_code_hash

    missing = tmp_path / "missing"
    regular_file = tmp_path / "regular-file"
    regular_file.write_text("not a package", encoding="utf-8")
    without_init = tmp_path / "without-init"
    without_init.mkdir()
    (without_init / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    os.symlink(outside, package / "linked.py")
    linked_root = tmp_path / "linked-root"
    os.symlink(package, linked_root, target_is_directory=True)

    for unsafe in (missing, regular_file, without_init, package, linked_root):
        with pytest.raises((ValueError, OSError)):
            calculate_code_hash(unsafe)


def test_code_hash_ignores_metadata_and_non_sources_but_detects_source_mutations(
    tmp_path: Path,
) -> None:
    from trading_research.provenance import calculate_code_hash

    root = tmp_path / "trading_research"
    root.mkdir()
    initializer = root / "__init__.py"
    initializer.write_text("", encoding="utf-8")
    module = root / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    original = calculate_code_hash(root)

    os.chmod(module, 0o600)
    os.utime(module, (1, 1))
    (root / "notes.txt").write_text("ignored", encoding="utf-8")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "shadow.py").write_text("ignored", encoding="utf-8")
    metadata_only = calculate_code_hash(root)

    module.write_text("VALUE = 2\n", encoding="utf-8")
    content_changed = calculate_code_hash(root)
    module.rename(root / "renamed.py")
    path_changed = calculate_code_hash(root)

    assert metadata_only == original
    assert content_changed != original
    assert path_changed != content_changed


def test_code_hash_fails_closed_if_selected_source_becomes_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research import provenance

    root = tmp_path / "trading_research"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    module = root / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    original_members = provenance._python_members

    def replace_after_selection(package_root: Path) -> tuple[Path, ...]:
        members = original_members(package_root)
        module.unlink()
        module.symlink_to(outside)
        return members

    monkeypatch.setattr(provenance, "_python_members", replace_after_selection)

    with pytest.raises(OSError):
        provenance.calculate_code_hash(root)
