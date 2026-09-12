"""Deterministic provenance for the installed Python source payload."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

_DOMAIN = b"trading-research-code-v1\0"
_LENGTH_BYTES = 8


@dataclass(frozen=True, slots=True)
class _SourceSnapshotMember:
    relative_path: str
    identity: tuple[int, int]
    payload: bytes


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    root_identity: tuple[int, int]
    directory_identities: tuple[tuple[str, tuple[int, int]], ...]
    members: tuple[_SourceSnapshotMember, ...]


def calculate_code_hash(package_root: str | Path | None = None) -> str:
    """Return the canonical SHA-256 identity of package Python sources."""

    root = Path(__file__).parent if package_root is None else Path(package_root)
    before = _snapshot_python_sources(root)
    if not any(member.relative_path == "__init__.py" for member in before.members):
        raise ValueError("package root must contain a regular __init__.py")

    digest = sha256(_DOMAIN)
    for member in before.members:
        relative = member.relative_path.encode("utf-8")
        digest.update(len(relative).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(relative)
        digest.update(len(member.payload).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(member.payload)
    after = _snapshot_python_sources(root)
    if after != before:
        raise OSError("Python package changed while calculating code hash")
    return digest.hexdigest()


def _snapshot_python_sources(root: Path) -> _SourceSnapshot:
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_status = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_status.st_mode):
            raise ValueError("package root must be a real directory")
        directories: list[tuple[str, tuple[int, int]]] = []
        members: list[_SourceSnapshotMember] = []
        _snapshot_directory(root_descriptor, (), directories, members)
    finally:
        os.close(root_descriptor)

    if not members:
        raise ValueError("package root contains no Python members")
    return _SourceSnapshot(
        root_identity=(root_status.st_dev, root_status.st_ino),
        directory_identities=tuple(sorted(directories)),
        members=tuple(sorted(members, key=lambda member: member.relative_path)),
    )


def _snapshot_directory(
    directory_descriptor: int,
    parent_parts: tuple[str, ...],
    directories: list[tuple[str, tuple[int, int]]],
    members: list[_SourceSnapshotMember],
) -> bool:
    contains_python = False
    for name in sorted(os.listdir(directory_descriptor)):
        if name in {".git", "__pycache__"}:
            continue
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError("unsafe package-relative Python path")
        status_at_path = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        relative_path = "/".join((*parent_parts, name))
        if stat.S_ISLNK(status_at_path.st_mode):
            if name.endswith(".py"):
                raise ValueError("symlinked package members are unsafe")
            try:
                target_status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=True)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(target_status.st_mode):
                raise ValueError("symlinked package directories are unsafe")
            continue
        if stat.S_ISDIR(status_at_path.st_mode):
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            try:
                opened_status = os.fstat(child_descriptor)
                identity = (opened_status.st_dev, opened_status.st_ino)
                if identity != (status_at_path.st_dev, status_at_path.st_ino):
                    raise OSError("Python package directory changed before it could be opened")
                child_contains_python = _snapshot_directory(
                    child_descriptor, (*parent_parts, name), directories, members
                )
                if child_contains_python:
                    directories.append((relative_path, identity))
                    contains_python = True
            finally:
                os.close(child_descriptor)
            continue
        if not name.endswith(".py"):
            continue
        if not stat.S_ISREG(status_at_path.st_mode):
            raise ValueError("Python package members must be regular files")
        identity, payload = _read_regular_file(directory_descriptor, name, status_at_path)
        members.append(
            _SourceSnapshotMember(
                relative_path=relative_path,
                identity=identity,
                payload=payload,
            )
        )
        contains_python = True
    return contains_python


def _read_regular_file(
    directory_descriptor: int, name: str, path_status: os.stat_result
) -> tuple[tuple[int, int], bytes]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise ValueError("Python package members must be regular files")
        identity = (opened_status.st_dev, opened_status.st_ino)
        if identity != (path_status.st_dev, path_status.st_ino):
            raise OSError("Python package member changed before it could be opened")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return identity, b"".join(chunks)
    finally:
        os.close(descriptor)
