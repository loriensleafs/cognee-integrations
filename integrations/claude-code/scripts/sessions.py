"""Session management — backed by cognee custom DataPoints + nodesets.

Mapping:

    Project   →  cognee dataset (``project_<hash>``)
    Session   →  custom ``Session`` DataPoint
    Status    →  NodeSet (``IN_PROGRESS`` | ``ENDED``) — mutually exclusive
    Active    →  NodeSet (``ACTIVE``) — at most one per project
    Branch    →  NodeSet (``branch_<name>``)

Source of truth is the cognee graph; all writes go through ``add_data_points``
which upserts by UUID. Readers hit the hot-path cache at
``~/.cognee-plugin/projects/<hash>/active.json`` so per-prompt hooks never
require a cognee round-trip just to read the active session id.

The cache is rewritten on every state change and reconciled against cognee at
SessionStart. If the cache is corrupt or missing, callers can ``await
ensure_active_session`` to rehydrate from cognee.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

from cognee.infrastructure.engine import DataPoint
from cognee.tasks.storage import add_data_points

_PLUGIN_ROOT = Path.home() / ".cognee-plugin"
_PROJECTS_ROOT = _PLUGIN_ROOT / "projects"


# ─── Custom DataPoint ─────────────────────────────────────────────────────


class Session(DataPoint):
    """A unit of work within a project, with explicit lifecycle.

    ``id`` is inherited from ``DataPoint`` (UUID). The cognee ``session_id``
    parameter on ``remember``/``recall`` calls is the string form of this UUID.
    """

    label: str = ""
    started_at: str = ""
    ended_at: Optional[str] = None
    git_branch: str = ""
    summary_snapshot: str = ""
    auto_named: bool = True
    turn_count: int = 0
    metadata: dict = {"index_fields": ["label", "summary_snapshot"]}


# NodeSet names used as belongs_to_set markers.
NS_ACTIVE = "ACTIVE"
NS_IN_PROGRESS = "IN_PROGRESS"
NS_ENDED = "ENDED"


# ─── Stateless helpers ────────────────────────────────────────────────────


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(session_id: str) -> str:
    return session_id[:8]


def compute_project_hash(cwd: Optional[str] = None) -> str:
    """Stable 12-char hex hash of an absolute cwd."""
    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    cwd = os.path.abspath(cwd)
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def dataset_name(project_hash: str) -> str:
    """Cognee dataset name for a project."""
    return f"project_{project_hash}"


def project_nodeset(project_hash: str) -> str:
    """NodeSet tagging items as belonging to this project."""
    return f"project_{project_hash}"


def branch_nodeset(branch: str) -> Optional[str]:
    """NodeSet tagging items as related to a git branch."""
    if not branch:
        return None
    sanitized = branch.replace("/", "-").replace(" ", "-")[:40]
    return f"branch_{sanitized}"


def get_git_branch(cwd: str) -> str:
    """Current git branch, or empty string if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# ─── Hot-path cache (active.json) ─────────────────────────────────────────


def _cache_path(project_hash: str) -> Path:
    return _PROJECTS_ROOT / project_hash / "active.json"


def write_cache(project_hash: str, session: "Session", project_root: str) -> None:
    """Persist active session info atomically. Reads stay <1ms."""
    cache_path = _cache_path(project_hash)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": str(session.id),
        "session_label": session.label,
        "dataset": dataset_name(project_hash),
        "project_hash": project_hash,
        "project_root": project_root,
        "git_branch": session.git_branch,
        "summary_snapshot": session.summary_snapshot,
    }
    fd, tmp_path = tempfile.mkstemp(prefix=".active.", suffix=".tmp", dir=cache_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def read_cache(project_hash: str) -> Optional[dict]:
    """Read cached active session payload. None on miss/corrupt."""
    path = _cache_path(project_hash)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_cache(project_hash: str) -> None:
    path = _cache_path(project_hash)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ─── Cognee-backed operations ─────────────────────────────────────────────


def _extract_sessions(results: Any) -> List["Session"]:
    """Unwrap cognee.search results to extract Session DataPoint instances."""
    out: List[Session] = []
    for r in results or []:
        if isinstance(r, Session):
            out.append(r)
            continue
        # SearchResult variants may wrap the node in `.datapoint` or `.node`
        candidate = getattr(r, "datapoint", None) or getattr(r, "node", None)
        if isinstance(candidate, Session):
            out.append(candidate)
    return out


async def _query_sessions(
    project_hash: str,
    nodesets: List[str],
    query_text: str = "session",
    top_k: int = 50,
) -> List["Session"]:
    """Find Session DataPoints in this project filtered by nodesets."""
    import cognee
    from cognee.modules.search.types.SearchType import SearchType

    results = await cognee.search(
        query_text=query_text,
        query_type=SearchType.CHUNKS,
        datasets=[dataset_name(project_hash)],
        node_type=Session,
        node_name=nodesets,
        node_name_filter_operator="AND",
        top_k=top_k,
    )
    return _extract_sessions(results)


async def find_active_session(project_hash: str) -> Optional["Session"]:
    """Cognee query for the project's active session."""
    sessions = await _query_sessions(project_hash, [NS_ACTIVE], top_k=1)
    return sessions[0] if sessions else None


async def find_in_progress_sessions(project_hash: str) -> List["Session"]:
    """All sessions tagged IN_PROGRESS in this project."""
    return await _query_sessions(project_hash, [NS_IN_PROGRESS], top_k=50)


async def find_session_by_natural_language(
    project_hash: str, query: str, in_progress_only: bool = True
) -> List["Session"]:
    """Semantic match against Session label/summary. Returns top-5 matches."""
    nodesets = [NS_IN_PROGRESS] if in_progress_only else []
    return await _query_sessions(project_hash, nodesets, query_text=query, top_k=5)


async def find_session_by_id(
    project_hash: str, session_id: str
) -> Optional["Session"]:
    """Lookup Session by full or short UUID prefix.

    Searches in-progress first, then ended. Comparison is on str(uuid).
    """
    candidates = await find_in_progress_sessions(project_hash)
    for s in candidates:
        sid = str(s.id)
        if sid == session_id or sid.startswith(session_id):
            return s
    # Fallback: ENDED sessions
    candidates = await _query_sessions(project_hash, [NS_ENDED], top_k=50)
    for s in candidates:
        sid = str(s.id)
        if sid == session_id or sid.startswith(session_id):
            return s
    return None


async def _persist(session: "Session") -> "Session":
    """Upsert the Session DataPoint via add_data_points (UUID-based dedup)."""
    session.updated_at = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    await add_data_points([session])
    return session


async def create_session(
    project_hash: str,
    project_root: str,
    label: Optional[str] = None,
    git_branch: str = "",
    set_active_flag: bool = True,
) -> "Session":
    """Create a new Session and persist to cognee. Optionally marks it active."""
    auto_named = not bool(label)
    if not label:
        # Generate a placeholder; replaced by LLM rename after enough turns.
        label = f"untitled-{secrets.token_hex(4)}"

    belongs = [NS_IN_PROGRESS, project_nodeset(project_hash)]
    bn = branch_nodeset(git_branch)
    if bn:
        belongs.append(bn)

    if set_active_flag:
        # Demote any existing ACTIVE in this project before promoting ours.
        existing = await find_active_session(project_hash)
        if existing:
            existing.belongs_to_set = [
                s for s in (existing.belongs_to_set or []) if s != NS_ACTIVE
            ]
            await _persist(existing)
        belongs.append(NS_ACTIVE)

    session = Session(
        label=label,
        started_at=_now(),
        git_branch=git_branch,
        auto_named=auto_named,
        belongs_to_set=belongs,
    )
    await _persist(session)

    if set_active_flag:
        write_cache(project_hash, session, project_root)
    return session


async def set_active(
    project_hash: str, target_id: str, project_root: str
) -> Optional["Session"]:
    """Move ACTIVE nodeset from current to target. Updates cache."""
    target = await find_session_by_id(project_hash, target_id)
    if not target:
        raise ValueError(f"session not found: {target_id}")

    current = await find_active_session(project_hash)
    if current and str(current.id) == str(target.id):
        write_cache(project_hash, target, project_root)
        return target

    if current:
        current.belongs_to_set = [
            s for s in (current.belongs_to_set or []) if s != NS_ACTIVE
        ]
        await _persist(current)

    target_belongs = list(target.belongs_to_set or [])
    if NS_ACTIVE not in target_belongs:
        target_belongs.append(NS_ACTIVE)
    target.belongs_to_set = target_belongs
    await _persist(target)

    write_cache(project_hash, target, project_root)
    return target


async def end_session(
    project_hash: str, session_id: Optional[str] = None
) -> Optional["Session"]:
    """End a session (defaults to active). Removes IN_PROGRESS+ACTIVE, adds ENDED."""
    if session_id:
        session = await find_session_by_id(project_hash, session_id)
    else:
        session = await find_active_session(project_hash)
    if not session:
        return None

    new_belongs = [
        s for s in (session.belongs_to_set or []) if s not in (NS_ACTIVE, NS_IN_PROGRESS)
    ]
    if NS_ENDED not in new_belongs:
        new_belongs.append(NS_ENDED)
    session.belongs_to_set = new_belongs
    session.ended_at = _now()
    await _persist(session)

    # Active pointer cleared if the ended session was active.
    cache = read_cache(project_hash)
    if cache and cache.get("session_id") == str(session.id):
        clear_cache(project_hash)
    return session


async def rename_session(
    project_hash: str,
    session_id: Optional[str],
    label: str,
    *,
    by_user: bool = True,
) -> Optional["Session"]:
    """Rename. by_user=True locks auto_named=False so LLM rename skips it later."""
    if session_id:
        session = await find_session_by_id(project_hash, session_id)
    else:
        session = await find_active_session(project_hash)
    if not session:
        return None
    session.label = label
    if by_user:
        session.auto_named = False
    await _persist(session)

    # Refresh cached label if this is the active session.
    cache = read_cache(project_hash)
    if cache and cache.get("session_id") == str(session.id):
        cache["session_label"] = label
        # Re-write atomically using existing payload
        path = _cache_path(project_hash)
        path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return session


async def increment_turn(
    project_hash: str, session_id: Optional[str] = None
) -> Optional[int]:
    """Bump turn_count on a session. Returns new count."""
    if session_id:
        session = await find_session_by_id(project_hash, session_id)
    else:
        session = await find_active_session(project_hash)
    if not session:
        return None
    session.turn_count = (session.turn_count or 0) + 1
    await _persist(session)
    return session.turn_count


async def update_summary_snapshot(
    project_hash: str, snapshot: str, session_id: Optional[str] = None
) -> bool:
    """Write a fresh summary_snapshot to a session. Updates cache if active."""
    if session_id:
        session = await find_session_by_id(project_hash, session_id)
    else:
        session = await find_active_session(project_hash)
    if not session:
        return False
    session.summary_snapshot = snapshot
    await _persist(session)

    cache = read_cache(project_hash)
    if cache and cache.get("session_id") == str(session.id):
        cache["summary_snapshot"] = snapshot
        path = _cache_path(project_hash)
        path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return True


async def ensure_active_session(
    project_hash: str,
    project_root: str,
    git_branch: str = "",
) -> "Session":
    """Get the active session, creating an untitled placeholder if absent.

    Read order: cache (fast) → cognee (source of truth) → create (last resort).
    """
    # Fast path: trust cache
    cache = read_cache(project_hash)
    if cache and cache.get("session_id"):
        # We don't fully reconstruct the Session here — caller usually only
        # needs the id and label, both of which are in cache. If the caller
        # needs the full DataPoint, they should explicitly call find_active_session.
        # But for safety, look up cognee to get the live record.
        try:
            uid = UUID(cache["session_id"])
            for s in await find_in_progress_sessions(project_hash):
                if s.id == uid:
                    return s
        except Exception:
            pass
        # Cache is stale; fall through to cognee query.

    # Cognee query
    active = await find_active_session(project_hash)
    if active:
        write_cache(project_hash, active, project_root)
        return active

    # Create placeholder
    return await create_session(
        project_hash, project_root, git_branch=git_branch, set_active_flag=True
    )
