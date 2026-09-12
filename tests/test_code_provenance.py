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
    original_reader = provenance._read_regular_file
    replaced = False

    def replace_after_selection(
        directory_descriptor: int, name: str, path_status: os.stat_result
    ) -> tuple[tuple[int, int], bytes]:
        nonlocal replaced
        if name == "module.py" and not replaced:
            module.unlink()
            module.symlink_to(outside)
            replaced = True
        return original_reader(directory_descriptor, name, path_status)

    monkeypatch.setattr(provenance, "_read_regular_file", replace_after_selection)

    with pytest.raises(OSError):
        provenance.calculate_code_hash(root)


def test_code_hash_fails_closed_if_source_is_added_after_initial_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research import provenance

    root = tmp_path / "trading_research"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    original_listdir = provenance.os.listdir
    added = False

    def add_after_initial_enumeration(directory_descriptor: int) -> list[str]:
        nonlocal added
        names = original_listdir(directory_descriptor)
        if not added:
            (root / "added.py").write_text("ADDED = True\n", encoding="utf-8")
            added = True
        return names

    monkeypatch.setattr(provenance.os, "listdir", add_after_initial_enumeration)

    with pytest.raises(OSError, match="changed while calculating code hash"):
        provenance.calculate_code_hash(root)


@pytest.mark.parametrize("mutation", ["remove", "rename", "replace", "symlink", "content"])
def test_code_hash_fails_closed_for_source_interleavings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from trading_research import provenance

    root = tmp_path / "trading_research"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    module = root / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    original_snapshot = provenance._snapshot_python_sources
    mutated = False

    def mutate_after_initial_snapshot(
        package_root: Path,
    ) -> tuple[provenance._SourceSnapshotMember, ...]:
        nonlocal mutated
        snapshot = original_snapshot(package_root)
        if not mutated:
            if mutation == "remove":
                module.unlink()
            elif mutation == "rename":
                module.rename(root / "renamed.py")
            elif mutation == "replace":
                replacement = root / "replacement"
                replacement.write_bytes(module.read_bytes())
                replacement.replace(module)
            elif mutation == "symlink":
                module.unlink()
                module.symlink_to(outside)
            else:
                module.write_text("VALUE = 2\n", encoding="utf-8")
            mutated = True
        return snapshot

    monkeypatch.setattr(provenance, "_snapshot_python_sources", mutate_after_initial_snapshot)

    with pytest.raises((ValueError, OSError)):
        provenance.calculate_code_hash(root)


@pytest.mark.parametrize(
    "mutation", ["metadata", "non-source", "non-source-directory", "non-source-symlink"]
)
def test_code_hash_accepts_non_source_interleavings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from trading_research import provenance

    root = tmp_path / "trading_research"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    module = root / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    original_snapshot = provenance._snapshot_python_sources
    mutated = False

    def mutate_after_initial_snapshot(
        package_root: Path,
    ) -> tuple[provenance._SourceSnapshotMember, ...]:
        nonlocal mutated
        snapshot = original_snapshot(package_root)
        if not mutated:
            if mutation == "metadata":
                os.chmod(module, 0o600)
                os.utime(module, (1, 1))
            elif mutation == "non-source-symlink":
                outside = tmp_path / "outside.txt"
                outside.write_text("ignored", encoding="utf-8")
                (root / "notes.txt").symlink_to(outside)
            elif mutation == "non-source-directory":
                assets = root / "assets"
                assets.mkdir()
                (assets / "fixture.txt").write_text("ignored", encoding="utf-8")
            else:
                (root / "notes.txt").write_text("ignored", encoding="utf-8")
            mutated = True
        return snapshot

    monkeypatch.setattr(provenance, "_snapshot_python_sources", mutate_after_initial_snapshot)

    assert provenance.calculate_code_hash(root) == _expected_code_hash(root)


def test_code_hash_fails_closed_if_package_root_becomes_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research import provenance

    root = tmp_path / "trading_research"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_snapshot = provenance._snapshot_python_sources
    replaced = False

    def replace_root_after_initial_snapshot(
        package_root: Path,
    ) -> tuple[provenance._SourceSnapshotMember, ...]:
        nonlocal replaced
        snapshot = original_snapshot(package_root)
        if not replaced:
            moved_root = tmp_path / "moved-package"
            root.rename(moved_root)
            root.symlink_to(moved_root, target_is_directory=True)
            replaced = True
        return snapshot

    monkeypatch.setattr(provenance, "_snapshot_python_sources", replace_root_after_initial_snapshot)

    with pytest.raises(OSError):
        provenance.calculate_code_hash(root)
