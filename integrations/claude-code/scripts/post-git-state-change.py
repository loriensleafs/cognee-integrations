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
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log
from config import ensure_cognee_ready, ensure_identity, load_config
import sessions as S

_MAIN_SHA_CACHE = lambda ph: Path.home() / ".cognee-plugin" / "projects" / ph / "main-sha.txt"
_MERGE_RECENCY_SECONDS = 600  # only end sessions for PRs merged in the last 10 min


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


def _git_main_sha(cwd: str) -> str:
    """Current sha of local 'main', or empty if no main ref."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "main"],
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


def _recently_merged_branches(cwd: str) -> list[str]:
    """Return PR head branch names merged into main within the last 10 min.

    Uses ``gh pr list --state merged --base main`` so we don't depend on the
    user having a local copy of the branch. Empty list if ``gh`` is missing,
    not authenticated, or the project isn't a GitHub repo.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--base",
                "main",
                "--limit",
                "20",
                "--json",
                "mergedAt,headRefName",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=_MERGE_RECENCY_SECONDS)
    branches: list[str] = []
    for pr in data:
        ts_str = pr.get("mergedAt") or ""
        head = pr.get("headRefName") or ""
        if not ts_str or not head:
            continue
        try:
            ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            branches.append(head)
    return branches


async def _maybe_end_merged_sessions(project_hash: str, cwd: str) -> list[str]:
    """If main advanced since last check, end sessions whose git_branch was
    just merged into main. Returns list of ended session labels.
    """
    cache_path = _MAIN_SHA_CACHE(project_hash)
    current_sha = _git_main_sha(cwd)
    prior_sha = ""
    if cache_path.exists():
        try:
            prior_sha = cache_path.read_text(encoding="utf-8").strip()
        except Exception:
            prior_sha = ""

    if not current_sha or current_sha == prior_sha:
        return []

    # Persist the new sha first so a flapping main only fires merge detection once.
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(current_sha + "\n", encoding="utf-8")
    except Exception:
        pass

    # First-run initialization: don't try to end anything historical.
    if not prior_sha:
        return []

    merged = _recently_merged_branches(cwd)
    if not merged:
        return []

    in_progress = await S.find_in_progress_sessions(project_hash)
    ended_labels: list[str] = []
    for s in in_progress:
        if not s.git_branch or s.git_branch not in merged:
            continue
        ended = await S.end_session(project_hash, str(s.id))
        if ended:
            ended_labels.append(ended.label)
            hook_log(
                "session_ended_via_pr_merge",
                {"session": str(ended.id)[:8], "branch": s.git_branch},
            )
    return ended_labels


async def _run() -> None:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)

    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    project_hash = S.compute_project_hash(cwd)

    # PR-merge auto-end runs first: a session bound to a freshly-merged
    # branch should be ended *before* we resolve the active session for
    # the current HEAD branch.
    merged_ended = await _maybe_end_merged_sessions(project_hash, cwd)
    if merged_ended:
        _emit(
            {
                "text": (
                    "## Sessions auto-ended on PR merge\n"
                    + "\n".join(f"- {label}" for label in merged_ended)
                )
            }
        )

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
