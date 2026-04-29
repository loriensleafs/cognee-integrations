#!/usr/bin/env python3
"""Project/session-aware wrapper for cognee remember/recall/search.

Subcommands:

    remember [--project <name|hash>] [--session <id|short|label>]
             [--node-set <ns>[,<ns>...]] "<text>"
    recall   [--project <name|hash>] [--session <id|short|label>]
             [--top-k N] [--scope session|trace|graph_context|graph]
             "<query>"
    search   [--project <name|hash>] [--session <id|short|label>]
             [--node-set <ns>[,<ns>...]] [--top-k N]
             "<query>"

When ``--project`` and/or ``--session`` are given, they take priority over
the resolver-derived active context. ``--project`` accepts the project's
human-readable name (substring-matched, case-insensitive) or its 12-char
project_hash. ``--session`` accepts the full UUID, the first 8 hex chars,
or a substring of the session's label (matched within the resolved
project).

When neither override is given, falls back to the resolver
(``sessions.resolve_active_project_for_hooks``) and the active session
within the resolved project.

All ``cognee.remember`` calls are tagged with ``node_set=[session_id, ...]``
so chunks/entities cognee extracts inherit the session scope.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from config import ensure_cognee_ready, ensure_identity, load_config
import sessions as S


# ─── Project + session resolvers ──────────────────────────────────────────


async def _resolve_project(override: Optional[str]) -> Optional[dict]:
    """Resolve ``--project`` flag. Returns the project payload or None."""
    if override:
        # Try hash first (12 hex chars), fall back to name fuzzy match.
        if len(override) == 12 and all(c in "0123456789abcdef" for c in override.lower()):
            project = await S.find_project_by_hash(override.lower())
            if project:
                return S._project_to_payload(project)
        matches = await S.find_project_by_name_query(override)
        if not matches:
            print(
                f"No project matched: {override!r}\n"
                "Run /cognee-memory:project-list to see what's registered.",
                file=sys.stderr,
            )
            return None
        if len(matches) > 1:
            print(f"Multiple projects matched {override!r}; be more specific:", file=sys.stderr)
            for p in matches:
                print(f"  {p.project_hash}  {p.name}", file=sys.stderr)
            return None
        return S._project_to_payload(matches[0])

    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    return await S.resolve_active_project_for_hooks(cwd)


async def _resolve_session(
    project_hash: str, override: Optional[str]
) -> Optional[str]:
    """Resolve ``--session`` flag within a project. Returns the session_id (UUID str)."""
    if override:
        # Try direct UUID/prefix lookup first.
        match = await S.find_session_by_id(project_hash, override)
        if match:
            return str(match.id)
        # Try natural-language match against label / summary.
        matches = await S.find_session_by_natural_language(
            project_hash, override, in_progress_only=False
        )
        if not matches:
            print(
                f"No session matched: {override!r}\n"
                "Run /cognee-memory:session-list to see this project's sessions.",
                file=sys.stderr,
            )
            return None
        if len(matches) > 1:
            print(f"Multiple sessions matched {override!r}; be more specific:", file=sys.stderr)
            for s in matches:
                print(f"  {str(s.id)[:8]}  {s.label}", file=sys.stderr)
            return None
        return str(matches[0].id)

    active = await S.find_active_session(project_hash)
    if active:
        return str(active.id)
    return None


async def _resolve_context(
    project_override: Optional[str], session_override: Optional[str]
) -> Optional[Tuple[dict, str]]:
    """Returns (project_payload, session_id) or None on resolution failure."""
    project = await _resolve_project(project_override)
    if not project:
        if project_override is None:
            print(
                "No active project. Either:\n"
                "  - run /cognee-memory:project-create to register one,\n"
                "  - or pass --project <name> explicitly.",
                file=sys.stderr,
            )
        return None
    session_id = await _resolve_session(project["project_hash"], session_override)
    if not session_id:
        return None
    return project, session_id


# ─── argparse ─────────────────────────────────────────────────────────────


def _add_context_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project", help="Override active project (name or 12-hex hash)")
    p.add_argument("--session", help="Override active session (UUID, 8-hex prefix, or label substring)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_remember = sub.add_parser("remember", help="Store data tagged with the active session")
    _add_context_args(p_remember)
    p_remember.add_argument("--node-set", help="Extra node_set tags (comma-separated)")
    p_remember.add_argument("text", nargs="+", help="Data to remember")

    p_recall = sub.add_parser("recall", help="Recall against session + graph")
    _add_context_args(p_recall)
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument(
        "--scope",
        default="session,graph_context",
        help="Comma-separated: session,trace,graph_context,graph (default: session,graph_context)",
    )
    p_recall.add_argument("query", nargs="+", help="Query text")

    p_search = sub.add_parser("search", help="Search the permanent graph (cognee.search)")
    _add_context_args(p_search)
    p_search.add_argument("--node-set", help="Filter by node_set name(s) (comma-separated)")
    p_search.add_argument("--top-k", type=int, default=5)
    p_search.add_argument("query", nargs="+", help="Query text")

    return parser


# ─── Command implementations ──────────────────────────────────────────────


async def cmd_remember(args: argparse.Namespace) -> int:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)

    ctx = await _resolve_context(args.project, args.session)
    if not ctx:
        return 1
    project, session_id = ctx

    text = " ".join(args.text)
    node_set = [session_id]
    if args.node_set:
        node_set.extend(t.strip() for t in args.node_set.split(",") if t.strip())

    import cognee
    from _plugin_common import resolve_user, load_resolved

    resolved = load_resolved()
    user = await resolve_user(resolved.get("user_id", ""))

    result = await cognee.remember(
        text,
        dataset_name=S.dataset_name(project["project_hash"]),
        session_id=session_id,
        node_set=node_set,
        user=user,
    )
    print(
        f"Remembered into project={project['project_name']} "
        f"session={session_id[:8]} ({len(text)} chars)"
    )
    if result and hasattr(result, "entry_id"):
        print(f"entry_id: {result.entry_id}")
    return 0


async def cmd_recall(args: argparse.Namespace) -> int:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)

    ctx = await _resolve_context(args.project, args.session)
    if not ctx:
        return 1
    project, session_id = ctx

    query = " ".join(args.query)
    scope = [s.strip() for s in args.scope.split(",") if s.strip()]

    import cognee

    results = await cognee.recall(
        query,
        session_id=session_id,
        datasets=[S.dataset_name(project["project_hash"])],
        top_k=args.top_k,
        scope=scope,
    )
    if not results:
        print("(no recall matches)")
        return 0

    print(f"## Recall results ({len(results)})\n")
    for i, r in enumerate(results, 1):
        if not isinstance(r, dict):
            print(f"{i}. {r}")
            continue
        src = r.get("_source", "")
        if r.get("question") or r.get("answer"):
            q = r.get("question") or ""
            a = r.get("answer") or ""
            print(f"{i}. [{src}] Q: {q[:120]}")
            if a:
                print(f"   A: {a[:300]}")
        else:
            content = r.get("content") or r.get("text") or json.dumps(r, default=str)
            print(f"{i}. [{src}] {str(content)[:300]}")
        print()
    return 0


async def cmd_search(args: argparse.Namespace) -> int:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)

    ctx = await _resolve_context(args.project, args.session)
    if not ctx:
        return 1
    project, _session_id = ctx

    query = " ".join(args.query)
    node_names = (
        [t.strip() for t in args.node_set.split(",") if t.strip()]
        if args.node_set
        else None
    )

    import cognee
    from cognee.modules.engine.models.node_set import NodeSet

    kwargs: dict = {
        "query_text": query,
        "datasets": [S.dataset_name(project["project_hash"])],
        "top_k": args.top_k,
    }
    if node_names:
        kwargs["node_type"] = NodeSet
        kwargs["node_name"] = node_names

    results = await cognee.search(**kwargs)
    if not results:
        print("(no graph matches)")
        return 0

    print(f"## Graph search results ({len(results)})\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {str(r)[:400]}")
        print()
    return 0


_COMMANDS = {
    "remember": cmd_remember,
    "recall": cmd_recall,
    "search": cmd_search,
}


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    handler = _COMMANDS[args.cmd]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
