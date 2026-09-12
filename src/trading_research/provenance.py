"""Deterministic provenance for the installed Python source payload."""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path

_DOMAIN = b"trading-research-code-v1\0"
_LENGTH_BYTES = 8


def calculate_code_hash(package_root: str | Path | None = None) -> str:
    """Return the canonical SHA-256 identity of package Python sources."""

    root = Path(__file__).parent if package_root is None else Path(package_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("package root must be a real directory")
    initializer = root / "__init__.py"
    if initializer.is_symlink() or not initializer.is_file():
        raise ValueError("package root must contain a regular __init__.py")

    digest = sha256(_DOMAIN)
    members = _python_members(root)
    for member in members:
        relative_path = member.relative_to(root)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ValueError("unsafe package-relative Python path")
        relative = relative_path.as_posix().encode("utf-8")
        payload = _read_regular_file(member)
        digest.update(len(relative).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _read_regular_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Python package members must be regular files")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _python_members(root: Path) -> tuple[Path, ...]:
    members: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in {".git", "__pycache__"})
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise ValueError("symlinked package directories are unsafe")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            member = Path(directory) / name
            if not stat.S_ISREG(member.lstat().st_mode):
                raise ValueError("Python package members must be regular files")
            members.append(member)
    if not members:
        raise ValueError("package root contains no Python members")
    return tuple(sorted(members, key=lambda path: path.relative_to(root).as_posix()))
