#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Write resolved session ID to env cache for other hooks

The resolved session ID and dataset are written to a cache file
so that the other hook scripts (which run in separate processes)
can pick them up without re-computing.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from config import (
    ensure_cognee_ready,
    ensure_identity,
    get_dataset,
    get_session_id,
    load_config,
    save_config,
)

_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
_WATCHER_PID = Path.home() / ".cognee-plugin" / "watcher.pid"
_WATCHER_STOP = Path.home() / ".cognee-plugin" / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn_idle_watcher(session_id: str, dataset: str, config: dict) -> None:
    """Launch the idle watcher as a detached background process.

    Idempotent: if a watcher is already alive (from an earlier session
    on the same machine), we kill it so the new one picks up the new
    session. Launched with its own session via ``start_new_session=True``
    so it survives the parent shell closing.
    """
    if _watcher_alive():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    # Clear any stale stop sentinel from a previous run.
    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception:
        pass

    # Only the non-secret surface of config needs to travel — the
    # watcher re-runs ``ensure_cognee_ready`` on its own.
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "config": {
            "service_url": config.get("service_url", ""),
            "api_key": config.get("api_key", ""),
            "llm_api_key": config.get("llm_api_key", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = Path.home() / ".cognee-plugin" / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception:
        log_fh = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
        print("cognee-plugin: idle watcher started", file=sys.stderr)
    except Exception as e:
        print(f"cognee-plugin: idle watcher launch failed ({e})", file=sys.stderr)


def _write_resolved(
    session_id: str, dataset: str, user_id: str, cwd: str, api_key: str = ""
) -> None:
    """Cache resolved session ID, dataset, user ID, and API key for other hook scripts."""
    _RESOLVED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "cwd": cwd,
    }
    if api_key:
        data["api_key"] = api_key
    _RESOLVED_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _start():
    config = load_config()
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    # Configure cognee (cloud or local) — must happen before any DataPoint write.
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Register agent identity (claude-code@cognee.agent)
    user_id = ""
    agent_api_key = ""
    try:
        user_id, agent_api_key = await ensure_identity(config)
    except Exception as e:
        print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    # Resolve the active project + session. Resolver order (cwd is the
    # strongest signal — explicit user navigation beats prior sticky state):
    #   1. cwd → registered Project? Use it. Promote to active so subsequent
    #      hooks see consistent state and the sticky cache stays current.
    #      This means "cd into project A, restart Claude Code" auto-switches
    #      out of any prior sticky project B without a slash command.
    #   2. Else, sticky active-project pointer (covers the "I'm in a parent
    #      dir, keep working on the project I set last time" case).
    #   3. Else, no project — emit a high-visibility "register a project"
    #      message and fall back to legacy session ids so hooks don't crash.
    #
    # Projects are NEVER auto-created — the user must run
    # /cognee-memory:project-create to register one explicitly.
    import sessions as _S

    project_payload = await _S.resolve_active_project_for_hooks(cwd)

    # Always reconcile sticky cache to whatever the resolver returned. If cwd
    # picked a different project than was previously sticky, the cwd-match wins
    # and we promote the cwd project to active. If sticky and resolver agree,
    # set_active_project is a no-op.
    if project_payload:
        sticky = _S.read_active_project_cache()
        if not sticky or sticky.get("project_hash") != project_payload["project_hash"]:
            promoted = await _S.set_active_project(project_payload["project_hash"])
            if promoted:
                project_payload = _S.read_active_project_cache() or project_payload

    git_branch = _S.get_git_branch(cwd)
    active_label = ""
    active_summary = ""
    project_unregistered = project_payload is None

    if project_unregistered:
        # Memory hooks become best-effort no-ops until the user registers
        # a project. We still write resolved.json with legacy strings so the
        # rest of the hook chain doesn't crash on missing keys.
        session_id = get_session_id(config, cwd)
        dataset = get_dataset(config)
        print(
            "cognee-plugin: no active project — run /cognee-memory:project-create",
            file=sys.stderr,
        )
    else:
        project_hash = project_payload["project_hash"]
        project_root = project_payload.get("project_root", cwd)
        try:
            active = await _S.ensure_active_session(
                project_hash, project_root=project_root, git_branch=git_branch
            )
            session_id = str(active.id)
            dataset = _S.dataset_name(project_hash)
            active_label = active.label
            active_summary = active.summary_snapshot or ""
        except Exception as e:
            print(
                f"cognee-plugin: session resolution fell back to legacy ({e})",
                file=sys.stderr,
            )
            session_id = get_session_id(config, cwd)
            dataset = get_dataset(config)

    # Write resolved values for other hooks
    _write_resolved(session_id, dataset, user_id, cwd, api_key=agent_api_key)

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    # Launch the idle watcher. If COGNEE_IDLE_DISABLED is set, skip it.
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in ("1", "true", "yes"):
        _spawn_idle_watcher(session_id, dataset, config)

    mode = "cloud" if config.get("service_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    # Build the SessionStart context payload. Hydrates the conversation with
    # the active project + session label + persisted summary_snapshot so the
    # agent picks up where work left off, even on a fresh Claude Code process.
    if project_unregistered:
        session_section = (
            "## ⚠ No active project for this directory\n"
            f"`cwd` is `{cwd}`, which has no registered project.\n\n"
            "Memory hooks are running in degraded mode (no recall, no graph "
            "writes) until a project is registered. To start using cognee "
            "memory, run:\n\n"
            "    /cognee-memory:project-create\n\n"
            "It will ask you for a project name and capture the current cwd "
            "+ git remote as the project root. To work on a project that's "
            "already registered, run `/cognee-memory:project-activate "
            "<name>` instead."
        )
    else:
        project_name = project_payload.get("project_name") or "(unnamed)"
        project_root = project_payload.get("project_root") or cwd
        session_section = (
            f"## Active project\n"
            f"**{project_name}** (root: `{project_root}`)\n\n"
            f"## Active session\n"
            f"**{active_label}** (id={session_id[:8]})"
        )
        if git_branch:
            session_section += f" — branch: `{git_branch}`"
        if active_summary:
            session_section += f"\n\n### Where this session left off\n{active_summary}"

    routing = (
        "## Cognee Memory Connected\n"
        f"Mode: {mode} | Dataset: {dataset}\n\n"
        "Cognee organizes knowledge into three categories. "
        "When storing data with /cognee-memory:cognee-remember, "
        "route to the correct category:\n\n"
        "- **user_context** — user preferences, corrections, personal facts, "
        "communication style. Use when the user says 'remember my preference', "
        "'I always want', or shares personal details.\n"
        "- **project_docs** — repository docs, code context, architecture decisions, "
        "company data. Use when storing codebase knowledge, API docs, or project context.\n"
        "- **agent_actions** — reasoning traces, conclusions, discovered patterns. "
        "Use when you want to persist your own findings. "
        "Routine tool call logging is automatic (no action needed).\n\n"
        "When searching with /cognee-memory:cognee-search, you can filter by category "
        "using --node-set (user_context, project_docs, or agent_actions).\n"
        "If unsure which category, default to project_docs."
    )

    guidance = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "systemMessage": session_section + "\n\n" + routing,
        }
    }
    print(json.dumps(guidance))


def main():
    # Read stdin (SessionStart payload) — consumed but not used
    sys.stdin.read()

    try:
        asyncio.run(_start())
    except Exception as exc:
        print(f"cognee-plugin: session start failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
