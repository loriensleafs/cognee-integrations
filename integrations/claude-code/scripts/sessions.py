"""Per-project session registry.

Each project (identified by cwd hash) has its own ``sessions.json`` registry
storing the catalogue of sessions and an ``active`` pointer.

Lifecycle and invariants:

  - status: ``in_progress`` | ``ended``
  - is_active: at most one ``true`` per project; implies status ``in_progress``
  - status ``ended`` implies ``ended_at`` is set and ``is_active`` is false

Reads are cheap (small JSON file). Writes are atomic via tempfile + rename.

Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional

_PLUGIN_ROOT = Path.home() / ".cognee-plugin"
_PROJECTS_ROOT = _PLUGIN_ROOT / "projects"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_session_id() -> str:
    """16 hex chars (8 bytes of entropy). Short form is first 8."""
    return secrets.token_hex(8)


def _short(session_id: str) -> str:
    return session_id[:8]


def compute_project_hash(cwd: Optional[str] = None) -> str:
    """Stable 12-char hex hash of an absolute cwd."""
    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    cwd = os.path.abspath(cwd)
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def _project_dir(project_hash: str) -> Path:
    return _PROJECTS_ROOT / project_hash


def _registry_path(project_hash: str) -> Path:
    return _project_dir(project_hash) / "sessions.json"


def _empty_registry(project_hash: str, project_root: str) -> dict:
    return {
        "project_hash": project_hash,
        "project_name": Path(project_root).name,
        "project_root": project_root,
        "active": None,
        "sessions": {},
    }


def load_registry(project_hash: str, project_root: Optional[str] = None) -> dict:
    """Load the registry, creating an empty one on first read."""
    path = _registry_path(project_hash)
    if not path.exists():
        if project_root is None:
            project_root = os.environ.get("CLAUDE_CWD", os.getcwd())
        return _empty_registry(project_hash, project_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if project_root is None:
            project_root = os.environ.get("CLAUDE_CWD", os.getcwd())
        return _empty_registry(project_hash, project_root)


def save_registry(project_hash: str, registry: dict) -> None:
    """Atomic write — tempfile + rename."""
    project_dir = _project_dir(project_hash)
    project_dir.mkdir(parents=True, exist_ok=True)
    target = _registry_path(project_hash)
    fd, tmp_path = tempfile.mkstemp(prefix=".sessions.", suffix=".tmp", dir=project_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def dataset_name(project_hash: str, session_id: str) -> str:
    """Cognee dataset name for a session."""
    return f"project_{project_hash}_session_{_short(session_id)}"


def get_active_session(registry: dict) -> Optional[dict]:
    """Returns the active session record or None."""
    active_id = registry.get("active")
    if not active_id:
        return None
    return registry.get("sessions", {}).get(active_id)


def get_in_progress_sessions(registry: dict) -> list[dict]:
    """All sessions whose status is in_progress, sorted by started_at desc."""
    sessions = [s for s in registry.get("sessions", {}).values() if s.get("status") == "in_progress"]
    sessions.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return sessions


def get_session_by_id(registry: dict, session_id: str) -> Optional[dict]:
    """Lookup by full id or short id (first 8 chars)."""
    sessions = registry.get("sessions", {})
    if session_id in sessions:
        return sessions[session_id]
    for sid, s in sessions.items():
        if sid.startswith(session_id):
            return s
    return None


def create_session(
    registry: dict,
    label: Optional[str] = None,
    git_branch: Optional[str] = None,
    auto_named: bool = True,
    set_active: bool = True,
) -> dict:
    """Add a new session to the registry. Returns the session record (mutates registry)."""
    session_id = _new_session_id()
    if not label:
        label = f"untitled-{_short(session_id)}"
        auto_named = True
    record = {
        "id": session_id,
        "label": label,
        "auto_named": auto_named,
        "status": "in_progress",
        "is_active": False,
        "started_at": _now(),
        "ended_at": None,
        "git_branch": git_branch or "",
        "summary_snapshot": "",
        "turn_count": 0,
    }
    registry.setdefault("sessions", {})[session_id] = record
    if set_active:
        _activate(registry, session_id)
    return record


def _activate(registry: dict, session_id: str) -> None:
    """Set ``is_active`` on this session and clear it on every other.

    Enforces invariant: at most one active; active implies in_progress.
    """
    target = registry.get("sessions", {}).get(session_id)
    if not target:
        raise ValueError(f"session not found: {session_id}")
    if target.get("status") != "in_progress":
        raise ValueError(f"cannot activate {session_id}: status is {target.get('status')}")
    for sid, s in registry.get("sessions", {}).items():
        s["is_active"] = sid == session_id
    registry["active"] = session_id


def set_active(project_hash: str, session_id: str) -> dict:
    """Switch active session. Persists. Returns the activated record."""
    registry = load_registry(project_hash)
    full_id = session_id
    if not registry.get("sessions", {}).get(session_id):
        match = get_session_by_id(registry, session_id)
        if not match:
            raise ValueError(f"session not found: {session_id}")
        full_id = match["id"]
    _activate(registry, full_id)
    save_registry(project_hash, registry)
    return registry["sessions"][full_id]


def end_session(project_hash: str, session_id: Optional[str] = None) -> Optional[dict]:
    """End a session (defaults to active). Clears active if it was active.

    Returns the ended record, or None if nothing to end.
    """
    registry = load_registry(project_hash)
    if session_id is None:
        session_id = registry.get("active")
        if not session_id:
            return None
    record = get_session_by_id(registry, session_id)
    if not record:
        return None
    record["status"] = "ended"
    record["is_active"] = False
    record["ended_at"] = _now()
    if registry.get("active") == record["id"]:
        registry["active"] = None
    save_registry(project_hash, registry)
    return record


def rename_session(
    project_hash: str,
    session_id: Optional[str],
    label: str,
    *,
    by_user: bool = True,
) -> Optional[dict]:
    """Rename a session. ``by_user=True`` locks ``auto_named`` to false."""
    registry = load_registry(project_hash)
    if session_id is None:
        session_id = registry.get("active")
        if not session_id:
            return None
    record = get_session_by_id(registry, session_id)
    if not record:
        return None
    record["label"] = label
    if by_user:
        record["auto_named"] = False
    save_registry(project_hash, registry)
    return record


def increment_turn(project_hash: str, session_id: Optional[str] = None) -> Optional[int]:
    """Bump turn_count on a session. Returns the new count."""
    registry = load_registry(project_hash)
    if session_id is None:
        session_id = registry.get("active")
        if not session_id:
            return None
    record = registry.get("sessions", {}).get(session_id)
    if not record:
        return None
    record["turn_count"] = int(record.get("turn_count", 0)) + 1
    save_registry(project_hash, registry)
    return record["turn_count"]


def update_summary_snapshot(
    project_hash: str,
    snapshot: str,
    session_id: Optional[str] = None,
) -> bool:
    """Write a fresh summary_snapshot. Returns True if written."""
    registry = load_registry(project_hash)
    if session_id is None:
        session_id = registry.get("active")
        if not session_id:
            return False
    record = registry.get("sessions", {}).get(session_id)
    if not record:
        return False
    record["summary_snapshot"] = snapshot
    save_registry(project_hash, registry)
    return True


def ensure_active_session(
    project_hash: str,
    project_root: Optional[str] = None,
    git_branch: Optional[str] = None,
) -> dict:
    """Get the active session, creating one with a placeholder label if absent.

    The first time a project is seen, this creates ``untitled-<short>`` and marks
    it active. Subsequent calls just return the existing active record.
    """
    registry = load_registry(project_hash, project_root)
    active = get_active_session(registry)
    if active:
        return active
    record = create_session(registry, git_branch=git_branch)
    save_registry(project_hash, registry)
    return record
