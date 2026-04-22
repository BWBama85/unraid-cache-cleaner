"""Filesystem scanning."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .models import FileRecord
from .planner import collapse_roots, is_within_any, normalize_path


def _matches_glob(path: Path, patterns: tuple[str, ...]) -> bool:
    basename = path.name
    full_path = str(path)
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch.fnmatch(full_path, pattern):
                return True
            continue
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def scan_filesystem(
    watch_roots: tuple[Path, ...],
    excluded_globs: tuple[str, ...],
    *,
    protected_dirs: tuple[Path, ...] = (),
) -> list[FileRecord]:
    """Recursively scan files while skipping protected directories."""

    results: list[FileRecord] = []
    normalized_roots = collapse_roots(watch_roots)
    normalized_protected_dirs = tuple(normalize_path(path) for path in protected_dirs)

    for root in normalized_roots:
        if not root.exists() or not root.is_dir():
            continue

        for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = normalize_path(current_root)
            if is_within_any(current_path, normalized_protected_dirs):
                dirs[:] = []
                continue

            pruned_dirs: list[str] = []
            for directory in dirs:
                candidate = normalize_path(current_path / directory)
                try:
                    is_symlink = candidate.is_symlink()
                except OSError:
                    continue
                if is_symlink:
                    continue
                if is_within_any(candidate, normalized_protected_dirs):
                    continue
                pruned_dirs.append(directory)
            dirs[:] = pruned_dirs

            for filename in files:
                file_path = normalize_path(current_path / filename)
                try:
                    is_symlink = file_path.is_symlink()
                except OSError:
                    continue
                if is_symlink:
                    continue
                if _matches_glob(file_path, excluded_globs):
                    continue
                try:
                    stat_result = file_path.stat()
                except OSError:
                    continue
                results.append(
                    FileRecord(
                        path=file_path,
                        size=stat_result.st_size,
                        mtime=stat_result.st_mtime,
                    )
                )

    return results
