#!/usr/bin/env python3
"""CLI surface for the four /cognee-memory:project-* slash commands.

Subcommands:

    list                              — show all registered projects, mark active
    create <name> [<path>]            — register a new Project (path defaults to cwd)
    activate <natural-language>       — fuzzy-match a Project by name; mark active;
                                         demote previously-active; auto-resolve
                                         session within (1 in_progress → activate;
                                         0 → create; 2+ → list candidates)
    rename <new-name>                 — rename the active project

Projects are explicit — never auto-created. ``create`` requires the user to
have provided a name (the slash command's description tells Claude to ask).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(__file__))
from config import ensure_cognee_ready, ensure_identity, load_config
import sessions as S


def _short(uid: str) -> str:
    return uid[:8]


def _fmt_project(p: "S.Project") -> str:
    flags = []
    if p.is_active:
        flags.append("ACTIVE")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    branch_str = f" · default-branch:{p.git_default_branch}" if p.git_default_branch else ""
    remote_str = f" · {p.git_remote_url}" if p.git_remote_url else ""
    return (
        f"  {p.project_hash}  {p.name}{flag_str}\n"
        f"           root: {p.project_root}{branch_str}{remote_str}"
    )


async def _bootstrap() -> None:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)


async def cmd_list(_args: List[str]) -> int:
    await _bootstrap()
    projects = await S.list_projects()
    if not projects:
        print("No projects registered yet.")
        print("Run /cognee-memory:project-create to register one.")
        return 0

    print("## Registered projects\n")
    # Active first
    active = [p for p in projects if p.is_active]
    others = [p for p in projects if not p.is_active]
    for p in active + others:
        print(_fmt_project(p))
    return 0


async def cmd_create(args: List[str]) -> int:
    if not args:
        print(
            "Usage: project-cli create <name> [<path>]\n"
            "       The slash-command surface should ask the user for the name.",
            file=sys.stderr,
        )
        return 2

    name = args[0]
    project_root = args[1] if len(args) > 1 else os.environ.get("CLAUDE_CWD", os.getcwd())
    project_root = os.path.abspath(project_root)
    if not os.path.isdir(project_root):
        print(f"Not a directory: {project_root}", file=sys.stderr)
        return 2

    await _bootstrap()
    project = await S.register_project(name, project_root, set_active=True)
    print(
        f"Registered project **{project.name}**\n"
        f"  hash:    {project.project_hash}\n"
        f"  root:    {project.project_root}\n"
        f"  remote:  {project.git_remote_url or '(no origin remote)'}\n"
        f"  default: {project.git_default_branch or '(unknown)'}\n"
        f"  status:  ACTIVE"
    )

    # Auto-resolve session within the new active project
    git_branch = S.get_git_branch(project_root)
    session = await S.ensure_active_session(
        project.project_hash, project_root=project.project_root, git_branch=git_branch
    )
    print(f"\nActive session: **{session.label}** (id={_short(str(session.id))})")
    return 0


async def cmd_activate(args: List[str]) -> int:
    if not args:
        print(
            "Usage: project-cli activate <natural-language description>",
            file=sys.stderr,
        )
        return 2
    query = " ".join(args).strip()

    await _bootstrap()
    matches = await S.find_project_by_name_query(query)
    if not matches:
        print(f"No project matched: {query!r}")
        all_p = await S.list_projects()
        if all_p:
            print("\nRegistered projects you could pick:")
            for p in all_p:
                print(_fmt_project(p))
        else:
            print("\n(no projects registered yet — run /cognee-memory:project-create)")
        return 1

    if len(matches) > 1:
        print(f"Multiple projects matched {query!r}. Be more specific:\n")
        for p in matches:
            print(_fmt_project(p))
        return 1

    target = matches[0]
    activated = await S.set_active_project(target.project_hash)
    if not activated:
        print(f"Failed to activate {target.name}", file=sys.stderr)
        return 1
    print(
        f"Project **{activated.name}** is now active "
        f"(root: `{activated.project_root}`)"
    )

    # Within the now-active project, resolve the session.
    git_branch = S.get_git_branch(activated.project_root)
    sessions_in_p = await S.find_in_progress_sessions(activated.project_hash)
    active_session: Optional[S.Session] = None
    for s in sessions_in_p:
        if s.is_active:
            active_session = s
            break

    if active_session:
        print(f"\nActive session: **{active_session.label}** "
              f"(id={_short(str(active_session.id))})")
        if active_session.summary_snapshot:
            print(f"\n## Where this session left off\n{active_session.summary_snapshot}")
        return 0

    if len(sessions_in_p) == 0:
        new_s = await S.create_session(
            activated.project_hash,
            project_root=activated.project_root,
            git_branch=git_branch,
        )
        print(f"\nNo in-progress sessions — auto-created **{new_s.label}** "
              f"(id={_short(str(new_s.id))}).")
        return 0

    if len(sessions_in_p) == 1:
        only = sessions_in_p[0]
        await S.set_active(activated.project_hash, str(only.id),
                           project_root=activated.project_root)
        print(f"\nActive session: **{only.label}** (id={_short(str(only.id))}) "
              f"(promoted; was the only in-progress one)")
        if only.summary_snapshot:
            print(f"\n## Where this session left off\n{only.summary_snapshot}")
        return 0

    # 2+ in_progress, none active — leave session undecided, list candidates.
    print(f"\n{len(sessions_in_p)} in-progress sessions in this project. Pick one:\n")
    for s in sessions_in_p:
        print(f"  {_short(str(s.id))}  {s.label}  · turns {s.turn_count}")
    print("\nUse /cognee-memory:session-activate <description> to pick one.")
    return 0


async def cmd_rename(args: List[str]) -> int:
    if not args:
        print("Usage: project-cli rename <new name>", file=sys.stderr)
        return 2
    name = " ".join(args).strip()
    if not name:
        return 2
    await _bootstrap()
    renamed = await S.rename_active_project(name)
    if not renamed:
        print("No active project to rename.")
        return 1
    print(f"Renamed active project → {renamed.name}")
    return 0


async def cmd_cleanup_ghosts(_args: List[str]) -> int:
    """Delete Project DataPoints with empty project_root (legacy auto-created
    ghosts from before explicit registration). Idempotent."""
    await _bootstrap()
    from cognee.infrastructure.databases.graph import get_graph_engine

    engine = await get_graph_engine()
    rows = await engine.query(
        """MATCH (p) WHERE p.type = 'Project'
           AND (p.project_root IS NULL OR p.project_root = '')
           RETURN p.project_hash AS h, p.name AS n""",
        {},
    )
    if not rows:
        print("No ghost projects found.")
        return 0
    print(f"Found {len(rows)} ghost project(s):")
    for r in rows:
        print(f"  {r.get('h', '?')} — {r.get('n', '(unnamed)')}")
    print()

    deleted = await engine.query(
        """MATCH (p) WHERE p.type = 'Project'
           AND (p.project_root IS NULL OR p.project_root = '')
           DETACH DELETE p
           RETURN count(*) AS n""",
        {},
    )
    n = 0
    for r in deleted or []:
        n = r.get("n") if isinstance(r, dict) else (r[0] if r else 0)
        break
    print(f"Deleted {n} ghost project(s).")
    return 0


_COMMANDS = {
    "list": cmd_list,
    "create": cmd_create,
    "activate": cmd_activate,
    "rename": cmd_rename,
    "cleanup-ghosts": cmd_cleanup_ghosts,
}


async def _main(argv: List[str]) -> int:
    if not argv or argv[0] not in _COMMANDS:
        print(f"Usage: project-cli {{{'|'.join(_COMMANDS)}}} [args...]", file=sys.stderr)
        return 2
    return await _COMMANDS[argv[0]](argv[1:])


def main() -> int:
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
