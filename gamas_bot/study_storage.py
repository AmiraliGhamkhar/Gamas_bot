"""Private filesystem storage for Study Pack assets.

Binary documents and media are stored on disk under a private directory rather
than in SQLite, with path-traversal protection and owner-only permissions.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "asset.bin"
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned[:120] or "asset.bin"


def save_private_asset(
    base_dir: Path,
    user_id: int,
    pack_id: int,
    filename: str,
    data: bytes,
) -> tuple[str, int, str]:
    """Write *data* to a private per-user/per-pack path and return
    ``(relative_storage_path, byte_size, sha256_hex)``.
    """
    root = Path(base_dir).resolve()
    target_dir = root / str(int(user_id)) / str(int(pack_id))
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    safe_name = f"{digest[:12]}_{_sanitize_filename(filename)}"
    target_path = (target_dir / safe_name).resolve()
    if not target_path.is_relative_to(root):
        raise ValueError("Invalid asset storage path")
    target_path.write_bytes(data)
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass
    rel_path = target_path.relative_to(root).as_posix()
    return rel_path, len(data), digest


def resolve_private_asset(base_dir: Path, storage_path: str) -> Path:
    """Resolve *storage_path* inside *base_dir*, rejecting path traversal."""
    root = Path(base_dir).resolve()
    candidate = (root / storage_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Invalid asset storage path")
    return candidate
