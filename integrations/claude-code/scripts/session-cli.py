#!/usr/bin/env python3
"""CLI surface for the four /cognee-memory:session-* slash commands.

Subcommands:

    list                       — show this project's sessions (active + in-progress + ended)
    end [<id>|<short>]         — end a session (defaults to active)
    rename <label>             — rename the active session (locks auto_named=False)
    activate <natural query>   — fuzzy-match an in-progress session and make it active

Each subcommand prints a short, human-readable result to stdout. The active
session's summary_snapshot is emitted on ``activate`` so the conversation is
hydrated immediately with the new active session's context.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import List, Optional

# Make sibling modules importable
sys.path.insert(0, os.path.dirname(__file__))
from config import ensure_cognee_ready, ensure_identity, load_config
import sessions as S


def _short(uid: str) -> str:
    return uid[:8]


def _fmt_session(s: "S.Session", active_id: Optional[str] = None) -> str:
    flags = []
    if active_id and str(s.id) == active_id:
        flags.append("ACTIVE")
    if "ENDED" in (s.belongs_to_set or []):
        flags.append("ENDED")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    branch_str = f" · branch:{s.git_branch}" if s.git_branch else ""
    name_kind = "(auto)" if s.auto_named else ""
    return (
        f"  {_short(str(s.id))}  {s.label}{flag_str}{branch_str} {name_kind}\n"
        f"           started {s.started_at} · turns {s.turn_count}"
    )


async def _bootstrap() -> tuple[str, str]:
    """Initialize cognee + agent identity and return (project_hash, project_root).

    Uses the resolver so session-cli operates on the active project (which
    may differ from cwd — e.g. when the user is in a parent directory but
    has a sticky active project set).
    """
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    payload = await S.resolve_active_project_for_hooks(cwd)
    if payload:
        return payload["project_hash"], payload.get("project_root", cwd)
    # No active project — fall through with cwd-derived hash so callers can
    # detect the empty-project case (find_active_session returns None).
    return S.compute_project_hash(cwd), cwd


async def cmd_list(_args: List[str]) -> int:
    project_hash, cwd = await _bootstrap()
    in_prog = await S.find_in_progress_sessions(project_hash)
    active = await S.find_active_session(project_hash)
    active_id = str(active.id) if active else None

    print(f"## Sessions for {os.path.basename(cwd)} (project_{project_hash})\n")

    if not in_prog and not active:
        print("(no sessions yet — one will be created on your next prompt)")
        return 0

    print("### In progress")
    if not in_prog:
        print("  (none)")
    else:
        for s in in_prog:
            print(_fmt_session(s, active_id))
    return 0


async def cmd_end(args: List[str]) -> int:
    project_hash, _ = await _bootstrap()
    target = args[0] if args else None
    ended = await S.end_session(project_hash, target)
    if not ended:
        print("No session to end (no active session, or id not found).")
        return 1
    print(f"Ended session {_short(str(ended.id))} — {ended.label}")
    return 0


async def cmd_rename(args: List[str]) -> int:
    if not args:
        print("Usage: session-cli rename <new label>", file=sys.stderr)
        return 2
    new_label = " ".join(args).strip()
    if not new_label:
        print("Refusing to rename to empty label.", file=sys.stderr)
        return 2

    project_hash, _ = await _bootstrap()
    renamed = await S.rename_session(project_hash, None, new_label, by_user=True)
    if not renamed:
        print("No active session to rename.")
        return 1
    print(f"Renamed active session → {renamed.label}")
    return 0


async def cmd_activate(args: List[str]) -> int:
    if not args:
        print("Usage: session-cli activate <natural-language description>", file=sys.stderr)
        return 2
    query = " ".join(args).strip()

    project_hash, cwd = await _bootstrap()
    matches = await S.find_session_by_natural_language(project_hash, query)
    if not matches:
        print(f"No in-progress session matched: {query!r}")
        in_prog = await S.find_in_progress_sessions(project_hash)
        if in_prog:
            print("\nIn-progress sessions you could pick:")
            for s in in_prog:
                print(_fmt_session(s))
        return 1

    if len(matches) > 1:
        print(f"Multiple in-progress sessions matched {query!r}. Be more specific:\n")
        for s in matches:
            print(_fmt_session(s))
        return 1

    target = matches[0]
    activated = await S.set_active(project_hash, str(target.id), project_root=cwd)
    print(f"Session **{activated.label}** is now active (id={_short(str(activated.id))}).")
    if activated.summary_snapshot:
        print("\n## Where this session left off\n")
        print(activated.summary_snapshot)
    return 0


_COMMANDS = {
    "list": cmd_list,
    "end": cmd_end,
    "rename": cmd_rename,
    "activate": cmd_activate,
}


async def _main(argv: List[str]) -> int:
    if not argv or argv[0] not in _COMMANDS:
        print(f"Usage: session-cli {{{'|'.join(_COMMANDS)}}} [args...]", file=sys.stderr)
        return 2
    return await _COMMANDS[argv[0]](argv[1:])


def main() -> int:
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
