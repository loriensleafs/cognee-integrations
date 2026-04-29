#!/usr/bin/env python3
"""React to git HEAD or refs/heads/main changes.

Hooked via FileChanged on ``.git/HEAD$|.git/refs/heads/main$`` so we catch
every branch transition source-agnostically (CLI, IDE, external shell,
rebase, merge), not just the ones routed through Claude Code's Bash tool.

Behavior on HEAD change (active branch transition):

    Active session exists in this project           → no-op (deliberate
                                                       selection wins)
    No active, 0 in-progress sessions on branch     → create + activate +
                                                       emit hydration
    No active, exactly 1 in-progress on branch      → silent activate +
                                                       emit hydration
    No active, 2+ in-progress on branch             → don't activate;
                                                       emit info listing
                                                       so the agent knows
                                                       what's available

The ``.git/refs/heads/main`` half is reserved for V2 PR-merge auto-end
(it fires when main advances, which can mean an external merge); for V1
this script just records the event in hook.log.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log
from config import ensure_cognee_ready, ensure_identity, load_config
import sessions as S


def _emit(payload: dict) -> None:
    """Inject additional context into the running conversation."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "FileChanged",
                    "additionalContext": payload["text"],
                }
            }
        )
    )


def _hydration_text(session, reason: str) -> str:
    parts = [
        f"## Active session changed",
        f"({reason})",
        f"**{session.label}** (id={str(session.id)[:8]}) — branch: `{session.git_branch}`",
    ]
    if session.summary_snapshot:
        parts.append("")
        parts.append("### Where this session left off")
        parts.append(session.summary_snapshot)
    return "\n".join(parts)


def _ambiguity_text(branch: str, sessions: list) -> str:
    lines = [
        f"## Branch `{branch}` has {len(sessions)} in-progress sessions",
        "No session was auto-activated. Use `/cognee-memory:session-activate <description>` to pick one:",
        "",
    ]
    for s in sessions:
        lines.append(
            f"- **{s.label}** (id={str(s.id)[:8]}) — turns {s.turn_count}"
        )
    return "\n".join(lines)


async def _run() -> None:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)

    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    project_hash = S.compute_project_hash(cwd)
    branch = S.get_git_branch(cwd)
    if not branch:
        hook_log("git_state_no_branch")
        return

    active = await S.find_active_session(project_hash)
    if active is not None:
        hook_log(
            "git_state_active_present",
            {"branch": branch, "active": str(active.id)[:8], "label": active.label},
        )
        return

    in_progress = await S.find_in_progress_sessions(project_hash)
    on_branch = [s for s in in_progress if s.git_branch == branch]

    if len(on_branch) == 0:
        created = await S.create_session(
            project_hash, project_root=cwd, git_branch=branch
        )
        hook_log(
            "git_state_created",
            {"branch": branch, "session": str(created.id)[:8]},
        )
        _emit({"text": _hydration_text(created, "new branch detected — fresh session created")})
        return

    if len(on_branch) == 1:
        target = on_branch[0]
        activated = await S.set_active(project_hash, str(target.id), project_root=cwd)
        hook_log(
            "git_state_activated",
            {"branch": branch, "session": str(activated.id)[:8], "label": activated.label},
        )
        _emit({"text": _hydration_text(activated, "branch's only in-progress session is now active")})
        return

    # 2+ in_progress on this branch — info-inject without auto-activating.
    hook_log(
        "git_state_ambiguous",
        {"branch": branch, "count": len(on_branch)},
    )
    _emit({"text": _ambiguity_text(branch, on_branch)})


def main() -> None:
    sys.stdin.read()
    try:
        asyncio.run(_run())
    except Exception as exc:
        hook_log("git_state_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
