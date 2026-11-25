"""Cross-platform path resolution for prestEEG user data and repo checkout detection.

When running from a repository checkout (or an editable ``pip install -e .``),
existing ``KEYS.txt`` and ``recordings/`` inside the repository are used
directly. When installed as a regular package and launched outside a repo
checkout, user data defaults to ``~/Documents/prestEEG`` so keys and
recordings are persisted in a standard writable user directory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ENV_KEYS_PATH = "PRESTEEG_KEYS_PATH"
ENV_DATA_DIR = "PRESTEEG_DATA_DIR"
_LEGACY_ENV_KEYS_PATH = "EPOCX_KEYS_PATH"
_LEGACY_ENV_DATA_DIR = "EPOCX_DATA_DIR"


def get_repo_root() -> Optional[Path]:
    """Return the repository root if running from source / editable install, else None."""
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent, here.parent.parent.parent):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() or (cwd / "KEYS.txt").is_file():
        return cwd
    return None


def get_user_data_dir() -> Path:
    """Return the directory where mutable user data (KEYS.txt, recordings) live."""
    override = (os.environ.get(ENV_DATA_DIR) or os.environ.get(_LEGACY_ENV_DATA_DIR) or "").strip()
    if override:
        return Path(override).expanduser()
    repo_root = get_repo_root()
    if repo_root is not None:
        return repo_root
    return Path.home() / "Documents" / "prestEEG"


def get_keys_path() -> Path:
    """Return the path to ``KEYS.txt``."""
    override = (os.environ.get(ENV_KEYS_PATH) or os.environ.get(_LEGACY_ENV_KEYS_PATH) or "").strip()
    if override:
        return Path(override).expanduser()
    return get_user_data_dir() / "KEYS.txt"


def get_recordings_dir() -> Path:
    """Return the default recordings directory."""
    return get_user_data_dir() / "recordings"
