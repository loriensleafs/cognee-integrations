"""Session management — backed by cognee custom DataPoints.

Domain model:

    Project DataPoint   ──:project──   Session DataPoint
    {name, project_hash}                 {label, status, is_active,
                                          started_at, ended_at, git_branch,
                                          summary_snapshot, auto_named,
                                          turn_count}

The Session's lifecycle and selection state live as **fields**, not nodeset
membership: ``status='IN_PROGRESS'|'ENDED'`` and ``is_active=true|false``.
This makes Cypher queries column-level rather than list-contains, and is
the pattern recommended in the cognee docs' "Session as first-class
entity" guide.

Project association is a real graph edge (Session.project → Project), so
we can traverse ``MATCH (s:Session)-[:project]->(p:Project {...})``
naturally instead of stuffing project membership into a string nodeset.

Content ingestion (cognee.remember / cognee.add) is tagged with
``node_set=[session_id]`` at the call site so chunks and entities cognee
extracts inherit the session scope. That gives recall a graph-level
filter, not just a QA-cache filter.

Source of truth is the cognee graph; a tiny ``active.json`` cache holds
just the active session id + dataset + project_hash so per-prompt hooks
don't need a graph query to find the active session.
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
from typing import Any, List, Literal, Optional
from uuid import UUID, uuid4

from pydantic import SkipValidation

from cognee.infrastructure.engine import DataPoint
from cognee.tasks.storage import add_data_points

_PLUGIN_ROOT = Path.home() / ".cognee-plugin"
_PROJECTS_ROOT = _PLUGIN_ROOT / "projects"


# ─── Custom DataPoints ────────────────────────────────────────────────────


class Project(DataPoint):
    """Logical container for a project's sessions.

    Projects are explicit — never auto-created. Registered via the
    ``project-create`` slash command which prompts the user for a name and
    captures the project root + git metadata. ``project_hash`` (a stable hash
    of the absolute project_root) is the identity field, so re-running
    ``register_project`` for the same root upserts.

    Active-project flag is sticky across Claude Code conversations so the user
    can work on this project from any cwd until they explicitly switch.
    """

    name: str = ""
    project_hash: str = ""
    project_root: str = ""
    git_remote_url: str = ""
    git_default_branch: str = ""
    is_active: bool = False
    metadata: dict = {
        "index_fields": ["name"],
        "identity_fields": ["project_hash"],
    }


class Session(DataPoint):
    """A unit of work within a project, with explicit lifecycle.

    ``str(self.id)`` is what we pass as cognee's ``session_id`` filter
    parameter and what we use as the ``node_set`` tag on content ingestion.
    """

    label: str = ""
    status: Literal["IN_PROGRESS", "ENDED"] = "IN_PROGRESS"
    is_active: bool = False
    started_at: str = ""
    ended_at: Optional[str] = None
    git_branch: str = ""
    summary_snapshot: str = ""
    auto_named: bool = True
    turn_count: int = 0

    # Edge field — assigning a Project instance creates a (Session)-[:project]->(Project) edge.
    project: SkipValidation[Any] = None

    metadata: dict = {"index_fields": ["label", "summary_snapshot"]}


# ─── Stateless helpers ────────────────────────────────────────────────────


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_project_hash(cwd: Optional[str] = None) -> str:
    """Stable 12-char hex hash of an absolute cwd."""
    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    cwd = os.path.abspath(cwd)
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def dataset_name(project_hash: str) -> str:
    """Cognee dataset name for a project."""
    return f"project_{project_hash}"


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


# ─── Cognee dataset + permissions ─────────────────────────────────────────


_AGENT_EMAIL = "claude-code@cognee.agent"


async def _get_agent_user():
    """Return the agent User (claude-code@cognee.agent), or default if unavailable."""
    from cognee.modules.users.methods import get_default_user, get_user_by_email

    user = await get_user_by_email(_AGENT_EMAIL)
    if user is not None:
        return user
    return await get_default_user()


async def _ensure_dataset(project_hash: str) -> None:
    """Idempotently create the project's cognee dataset and grant the agent user
    read/write/delete/share permissions on it.
    """
    from cognee.modules.data.methods.create_dataset import create_dataset
    from cognee.modules.users.permissions.methods import give_permission_on_dataset

    user = await _get_agent_user()
    if user is None:
        return
    dataset = await create_dataset(dataset_name(project_hash), user)
    for perm in ("read", "write", "delete", "share"):
        try:
            await give_permission_on_dataset(user, dataset.id, perm)
        except Exception:
            pass


# ─── Project: registration, lookup, active pointer ──────────────────────────


_ACTIVE_PROJECT_CACHE = _PLUGIN_ROOT / "active-project.json"


def read_active_project_cache() -> Optional[dict]:
    """Read sticky active-project pointer. None on miss."""
    if not _ACTIVE_PROJECT_CACHE.exists():
        return None
    try:
        return json.loads(_ACTIVE_PROJECT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_active_project_cache(project: "Project") -> None:
    """Persist sticky active-project pointer atomically."""
    _ACTIVE_PROJECT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_hash": project.project_hash,
        "project_root": project.project_root,
        "project_name": project.name,
        "git_remote_url": project.git_remote_url,
        "git_default_branch": project.git_default_branch,
    }
    fd, tmp_path = tempfile.mkstemp(
        prefix=".active-project.", suffix=".tmp", dir=_ACTIVE_PROJECT_CACHE.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, _ACTIVE_PROJECT_CACHE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def clear_active_project_cache() -> None:
    try:
        _ACTIVE_PROJECT_CACHE.unlink()
    except FileNotFoundError:
        pass


def get_git_remote(project_root: str) -> str:
    """Best-effort: return ``origin`` remote URL, or ''."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_git_default_branch(project_root: str) -> str:
    """Detect the repo's default branch (typically main or master)."""
    try:
        # symbolic-ref refs/remotes/origin/HEAD → "refs/remotes/origin/main"
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            if "/" in ref:
                return ref.rsplit("/", 1)[1]
    except Exception:
        pass
    # Fallbacks
    for candidate in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return ""


async def register_project(
    name: str, project_root: str, *, set_active: bool = True
) -> "Project":
    """Create a Project with explicit name + captured git metadata.

    Idempotent via identity_fields=['project_hash']: re-running for the same
    project_root upserts (and updates name/metadata if changed).

    By default also marks this Project active and demotes any prior active.
    """
    if not name or not project_root:
        raise ValueError("name and project_root are required")
    project_root = os.path.abspath(project_root)
    project_hash = hashlib.sha256(project_root.encode()).hexdigest()[:12]

    if set_active:
        existing = await find_active_project()
        if existing and existing.project_hash != project_hash:
            existing.is_active = False
            await add_data_points([existing])

    project = Project(
        name=name,
        project_hash=project_hash,
        project_root=project_root,
        git_remote_url=get_git_remote(project_root),
        git_default_branch=get_git_default_branch(project_root),
        is_active=bool(set_active),
    )
    await add_data_points([project])

    if set_active:
        write_active_project_cache(project)
    return project


def _props_to_project(props: dict) -> Optional["Project"]:
    if not props or props.get("type") != "Project":
        return None
    try:
        return Project(
            id=UUID(str(props["id"])) if "id" in props else uuid4(),
            name=str(props.get("name", "")),
            project_hash=str(props.get("project_hash", "")),
            project_root=str(props.get("project_root", "")),
            git_remote_url=str(props.get("git_remote_url", "")),
            git_default_branch=str(props.get("git_default_branch", "")),
            is_active=bool(props.get("is_active", False)),
            created_at=int(props.get("created_at", 0)),
            updated_at=int(props.get("updated_at", 0)),
        )
    except Exception:
        return None


async def _query_projects(where: str = "", params: Optional[dict] = None,
                          limit: int = 50) -> List["Project"]:
    from cognee.infrastructure.databases.graph import get_graph_engine

    base_where = "n.type = 'Project'"
    full_where = f"{base_where} AND ({where})" if where else base_where
    cypher = (
        "MATCH (n) "
        f"WHERE {full_where} "
        "RETURN properties(n) AS props "
        f"LIMIT {int(limit)}"
    )
    try:
        engine = await get_graph_engine()
        rows = await engine.query(cypher, params or {})
    except Exception:
        return []
    out: List[Project] = []
    for row in rows or []:
        props = row.get("props") if isinstance(row, dict) else (row[0] if row else None)
        p = _props_to_project(props)
        if p is not None:
            out.append(p)
    return out


async def find_active_project() -> Optional["Project"]:
    """Sticky active project for this user, if one is registered."""
    matches = await _query_projects(where="n.is_active = true", limit=1)
    return matches[0] if matches else None


async def find_project_by_hash(project_hash: str) -> Optional["Project"]:
    matches = await _query_projects(
        where="n.project_hash = $h", params={"h": project_hash}, limit=1
    )
    return matches[0] if matches else None


async def find_project_by_name_query(query: str) -> List["Project"]:
    """Substring match against Project.name (case-insensitive)."""
    matches = await _query_projects(
        where="toLower(n.name) CONTAINS $q",
        params={"q": (query or "").lower()},
        limit=10,
    )
    return matches


async def list_projects() -> List["Project"]:
    return await _query_projects(limit=100)


async def set_active_project(project_hash: str) -> Optional["Project"]:
    """Mark a project active, demoting any prior active. Updates cache."""
    target = await find_project_by_hash(project_hash)
    if not target:
        return None
    current = await find_active_project()
    if current and current.project_hash != project_hash:
        current.is_active = False
        await add_data_points([current])
    target.is_active = True
    await add_data_points([target])
    write_active_project_cache(target)
    return target


async def rename_active_project(name: str) -> Optional["Project"]:
    project = await find_active_project()
    if not project:
        return None
    project.name = name
    await add_data_points([project])
    cache = read_active_project_cache()
    if cache:
        cache["project_name"] = name
        _ACTIVE_PROJECT_CACHE.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return project


async def resolve_active_project_for_hooks(cwd: Optional[str] = None) -> Optional[dict]:
    """Resolver hooks call before any project-scoped operation.

    Order:
      1. Sticky active-project cache (persists across Claude Code sessions)
      2. Cognee active flag (matches cache 99% of the time)
      3. Cognee Project at cwd's project_hash, if registered
      4. None — caller should signal "no project; user must run /cognee-memory:project-create"
    """
    cache = read_active_project_cache()
    if cache and cache.get("project_hash"):
        return cache

    active = await find_active_project()
    if active:
        write_active_project_cache(active)
        return read_active_project_cache()

    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    cwd = os.path.abspath(cwd)
    cwd_hash = hashlib.sha256(cwd.encode()).hexdigest()[:12]
    cwd_project = await find_project_by_hash(cwd_hash)
    if cwd_project:
        # Don't auto-promote to active — that's a deliberate user choice.
        return {
            "project_hash": cwd_project.project_hash,
            "project_root": cwd_project.project_root,
            "project_name": cwd_project.name,
            "git_remote_url": cwd_project.git_remote_url,
            "git_default_branch": cwd_project.git_default_branch,
        }
    return None


# ─── Cognee-backed session queries ────────────────────────────────────────


def _props_to_session(props: dict) -> Optional["Session"]:
    """Reconstruct a Session DataPoint from a graph node's stored properties."""
    if not props or props.get("type") != "Session":
        return None
    try:
        return Session(
            id=UUID(str(props["id"])) if "id" in props else uuid4(),
            label=str(props.get("label", "")),
            status=str(props.get("status", "IN_PROGRESS")),
            is_active=bool(props.get("is_active", False)),
            started_at=str(props.get("started_at", "")),
            ended_at=props.get("ended_at"),
            git_branch=str(props.get("git_branch", "")),
            summary_snapshot=str(props.get("summary_snapshot", "")),
            auto_named=bool(props.get("auto_named", True)),
            turn_count=int(props.get("turn_count", 0)),
            created_at=int(props.get("created_at", 0)),
            updated_at=int(props.get("updated_at", 0)),
        )
    except Exception:
        return None


async def _query_sessions(
    project_hash: str, *, where: str = "", params: Optional[dict] = None, limit: int = 50
) -> List["Session"]:
    """Find Session DataPoints linked to this project via the :project edge.

    ``where`` is appended to the base ``MATCH ... WHERE`` clause for status /
    activity filtering. ``params`` extends the base parameter dict.
    """
    from cognee.infrastructure.databases.graph import get_graph_engine

    await _ensure_dataset(project_hash)

    base_where = "p.project_hash = $project_hash"
    full_where = f"{base_where} AND ({where})" if where else base_where
    cypher = (
        "MATCH (s)-[:project]->(p) "
        "WHERE s.type = 'Session' AND p.type = 'Project' "
        f"AND {full_where} "
        "RETURN properties(s) AS props "
        f"LIMIT {int(limit)}"
    )
    full_params = {"project_hash": project_hash, **(params or {})}

    try:
        engine = await get_graph_engine()
        rows = await engine.query(cypher, full_params)
    except Exception:
        return []

    sessions: List[Session] = []
    for row in rows or []:
        props = row.get("props") if isinstance(row, dict) else (row[0] if row else None)
        s = _props_to_session(props)
        if s is not None:
            sessions.append(s)
    return sessions


async def find_active_session(project_hash: str) -> Optional["Session"]:
    """The session with ``is_active=true`` for this project, or None."""
    sessions = await _query_sessions(
        project_hash, where="s.is_active = true", limit=1
    )
    return sessions[0] if sessions else None


async def find_in_progress_sessions(project_hash: str) -> List["Session"]:
    """Sessions with ``status='IN_PROGRESS'`` for this project."""
    return await _query_sessions(
        project_hash, where="s.status = 'IN_PROGRESS'", limit=50
    )


async def find_session_by_natural_language(
    project_hash: str, query: str, in_progress_only: bool = True
) -> List["Session"]:
    """Substring-match against Session label / summary in this project.

    For in-progress narrowing, filter on ``status``. The ``query`` is
    matched case-insensitively against ``label`` and ``summary_snapshot``.
    A real semantic search would use cognee's vector index over the
    indexed fields; for V1 this string-match is enough for typical NL
    descriptions.
    """
    where_parts = []
    params = {"q": (query or "").lower()}
    if in_progress_only:
        where_parts.append("s.status = 'IN_PROGRESS'")
    where_parts.append(
        "(toLower(s.label) CONTAINS $q OR toLower(s.summary_snapshot) CONTAINS $q)"
    )
    return await _query_sessions(
        project_hash, where=" AND ".join(where_parts), params=params, limit=10
    )


async def find_session_by_id(
    project_hash: str, session_id: str
) -> Optional["Session"]:
    """Lookup Session by full or short UUID prefix.

    Searches in-progress first, then ended.
    """
    candidates = await find_in_progress_sessions(project_hash)
    for s in candidates:
        sid = str(s.id)
        if sid == session_id or sid.startswith(session_id):
            return s
    candidates = await _query_sessions(
        project_hash, where="s.status = 'ENDED'", limit=50
    )
    for s in candidates:
        sid = str(s.id)
        if sid == session_id or sid.startswith(session_id):
            return s
    return None


# ─── Lifecycle writes ─────────────────────────────────────────────────────


async def _persist(session: "Session") -> "Session":
    """Upsert via add_data_points (UUID-based dedup)."""
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
    """Create a new Session linked to its Project. Optionally mark active."""
    auto_named = not bool(label)
    if not label:
        label = f"untitled-{secrets.token_hex(4)}"

    project = await find_project_by_hash(project_hash)
    if not project:
        raise ValueError(
            f"No Project registered for project_hash={project_hash}. "
            "Register one via /cognee-memory:project-create before creating sessions."
        )

    if set_active_flag:
        # Demote any existing ACTIVE in this project before promoting ours.
        existing = await find_active_session(project_hash)
        if existing:
            existing.is_active = False
            await _persist(existing)

    session = Session(
        label=label,
        status="IN_PROGRESS",
        is_active=bool(set_active_flag),
        started_at=_now(),
        git_branch=git_branch,
        auto_named=auto_named,
    )
    session.project = project  # creates (Session)-[:project]->(Project) edge

    await add_data_points([session])

    if set_active_flag:
        write_cache(project_hash, session, project_root)
    return session


async def set_active(
    project_hash: str, target_id: str, project_root: str
) -> Optional["Session"]:
    """Move active flag from current to target. Updates cache."""
    target = await find_session_by_id(project_hash, target_id)
    if not target:
        raise ValueError(f"session not found: {target_id}")

    current = await find_active_session(project_hash)
    if current and str(current.id) == str(target.id):
        write_cache(project_hash, target, project_root)
        return target

    if current:
        current.is_active = False
        await _persist(current)

    target.is_active = True
    await _persist(target)

    write_cache(project_hash, target, project_root)
    return target


async def end_session(
    project_hash: str, session_id: Optional[str] = None
) -> Optional["Session"]:
    """End a session (defaults to active). Sets status='ENDED', is_active=false, ended_at=now."""
    if session_id:
        session = await find_session_by_id(project_hash, session_id)
    else:
        session = await find_active_session(project_hash)
    if not session:
        return None

    session.status = "ENDED"
    session.is_active = False
    session.ended_at = _now()
    await _persist(session)

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

    cache = read_cache(project_hash)
    if cache and cache.get("session_id") == str(session.id):
        cache["session_label"] = label
        path = _cache_path(project_hash)
        path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return session


async def increment_turn(
    project_hash: str, session_id: Optional[str] = None
) -> Optional[int]:
    """Bump turn_count. Returns new count."""
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
    """Write a fresh summary_snapshot. Updates cache if active."""
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


# ─── LLM auto-naming ──────────────────────────────────────────────────────


_AUTO_RENAME_AT_TURN = 5
_AUTO_RENAME_PROMPT = (
    "Read the recent conversation between the user and the AI assistant below "
    "and produce a short label (4 to 7 words) that captures what the user is "
    "working on. Use Title Case, no quotes, no trailing punctuation. Output "
    "ONLY the label — no preface, no commentary."
)


async def _fetch_recent_qa(session_id: str, limit: int = 10) -> List[dict]:
    """Pull recent QA entries from cognee's session cache for naming context."""
    try:
        import cognee

        results = await cognee.session.get_session(
            session_id=session_id, last_n=limit, formatted=False
        )
        return list(results) if results else []
    except Exception:
        return []


def _format_qa_for_naming(entries: List[dict]) -> str:
    parts: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        q = str(e.get("question") or "").strip()
        a = str(e.get("answer") or "").strip()
        if q:
            parts.append(f"User: {q[:300]}")
        if a:
            parts.append(f"AI: {a[:300]}")
    return "\n".join(parts).strip()


async def _llm_label_from_text(blob: str) -> Optional[str]:
    """Call litellm.acompletion with the cognee-configured provider for a label."""
    if not blob:
        return None
    import litellm
    from cognee.infrastructure.llm import get_llm_config

    cfg = get_llm_config()
    try:
        resp = await litellm.acompletion(
            model=cfg.llm_model,
            messages=[
                {"role": "system", "content": _AUTO_RENAME_PROMPT},
                {"role": "user", "content": blob},
            ],
            max_tokens=40,
            temperature=0.2,
        )
    except Exception:
        return None
    try:
        text = resp.choices[0].message.content or ""
    except Exception:
        return None
    label = text.strip().strip('"').strip("'").strip()
    label = label.split("\n", 1)[0].strip()
    if not (3 <= len(label) <= 80):
        return None
    return label


async def auto_rename_if_due(project_hash: str) -> Optional["Session"]:
    """Rename the active session via Bedrock if it's still ``auto_named`` and
    has just hit the threshold turn count.
    """
    active = await find_active_session(project_hash)
    if not active or not active.auto_named:
        return None
    if int(active.turn_count or 0) != _AUTO_RENAME_AT_TURN:
        return None

    qa = await _fetch_recent_qa(str(active.id))
    blob = _format_qa_for_naming(qa)
    if not blob:
        return None

    label = await _llm_label_from_text(blob)
    if not label:
        return None
    return await rename_session(project_hash, str(active.id), label, by_user=False)


# ─── Bootstrap ────────────────────────────────────────────────────────────


async def ensure_active_session(
    project_hash: str,
    project_root: str,
    git_branch: str = "",
) -> "Session":
    """Get the active session, creating an untitled placeholder if absent.

    Read order: cache (fast) → cognee (source of truth) → create (last resort).
    """
    cache = read_cache(project_hash)
    if cache and cache.get("session_id"):
        try:
            uid = UUID(cache["session_id"])
            for s in await find_in_progress_sessions(project_hash):
                if s.id == uid and s.is_active:
                    return s
        except Exception:
            pass

    active = await find_active_session(project_hash)
    if active:
        write_cache(project_hash, active, project_root)
        return active

    return await create_session(
        project_hash, project_root, git_branch=git_branch, set_active_flag=True
    )
