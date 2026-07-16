#!/usr/bin/env python3
"""Safe JSON state-file helpers.

These helpers provide short-lived read → mutate → write transactions protected
by both an in-process lock and an OS file lock, so independent bot processes do
not overwrite each other's state updates.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_state_locks: dict[str, threading.Lock] = {}
_state_locks_guard = threading.Lock()


def _normalize_path(path) -> str:
    return os.path.abspath(os.fspath(path))


def _get_state_lock(path) -> threading.Lock:
    normalized = _normalize_path(path)
    with _state_locks_guard:
        lock = _state_locks.get(normalized)
        if lock is None:
            lock = threading.Lock()
            _state_locks[normalized] = lock
        return lock


def _new_default(default):
    return default() if callable(default) else default


def _load_json_safe(path, default=dict):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _new_default(default)
    return data


def atomic_write_json(path, data, *, ensure_ascii=False, indent=2):
    """Write JSON to `path` atomically (temp file in same dir + os.replace)."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=ensure_ascii, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_json_state(path, mutator: Callable[[T], None], *, default=dict, ensure_ascii=False, indent=2) -> T:
    """Thread-safe and process-safe read→mutate→write for a JSON state file.

    The lock is intentionally held only around local file I/O and the caller's
    in-memory mutation. Do not perform API calls or other external I/O inside
    the mutator.
    """
    path = _normalize_path(path)
    lock = _get_state_lock(path)
    lockfile = path + ".lock"
    with lock:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(lockfile, "a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                data = _load_json_safe(path, default)
                replacement = mutator(data)
                if replacement is not None:
                    data = replacement
                atomic_write_json(path, data, ensure_ascii=ensure_ascii, indent=indent)
                return data
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
