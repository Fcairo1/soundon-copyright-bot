#!/usr/bin/env python3
"""Shared filesystem path resolution for copyright_alert.

The AIME platform provides helper scripts (``feishu-im-send``,
``aeolus-platform-analysis``) under an ``inner_skills/`` directory that lives
OUTSIDE this repository — at the workspace root — not inside the repo checkout.

When scheduled tasks were relocated from the stale workspace-root
``copyright_alert/`` copy into the ``soundon-copyright-bot/`` repo
(2026-07-31), every hardcoded ``ROOT / "inner_skills"`` reference silently
broke: the repo root has no ``inner_skills/`` directory, so Aeolus lookups and
the ``feishu-im-send`` DM helper could no longer be found.

This module centralises the resolution so every caller locates the directory
regardless of the process working directory.

Resolution order:
  1. ``INNER_SKILLS_DIR`` environment variable (documented in README/.env.example).
  2. ``<repo>/inner_skills`` — useful when the directory is bind-mounted or
     symlinked into the checkout.
  3. ``<workspace_root>/inner_skills`` — the standard AIME layout where the
     repo is a direct child of the workspace root.
  4. Fallback to ``<repo>/inner_skills`` (preserves the documented default even
     if the directory is absent, so callers get a clear "file not found").
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root: soundon-copyright-bot/
REPO_ROOT = Path(__file__).resolve().parents[1]
# Workspace root (parent of the repo checkout in AIME).
WORKSPACE_ROOT = REPO_ROOT.parent


def inner_skills_dir() -> Path:
    """Return the absolute path to the AIME ``inner_skills/`` directory."""
    env = os.environ.get("INNER_SKILLS_DIR", "").strip()
    if env:
        candidate = Path(env).expanduser()
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = Path(os.path.abspath(os.path.expanduser(env)))
        if candidate.is_dir():
            return candidate

    repo_local = REPO_ROOT / "inner_skills"
    if repo_local.is_dir():
        return repo_local

    workspace = WORKSPACE_ROOT / "inner_skills"
    if workspace.is_dir():
        return workspace

    # Last-resort default; may not exist but preserves prior behaviour so the
    # resulting subprocess error message stays understandable.
    return repo_local


def inner_skill(*parts: str) -> Path:
    """Return an absolute path inside the resolved ``inner_skills/`` tree."""
    return inner_skills_dir().joinpath(*parts)
