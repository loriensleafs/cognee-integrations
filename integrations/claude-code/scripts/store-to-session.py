#!/usr/bin/env python3
"""Store tool calls and assistant responses into the Cognee session cache.

Routes tool calls to the structured ``TraceEntry`` path (new trace-step
shape with origin_function / method_params / method_return_value /
status). Routes the final assistant message on Stop to a ``QAEntry``.

Runs async on the PostToolUse / Stop hooks — fire-and-forget, never
blocks Claude.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
    Falls back to COGNEE_SESSION_ID / COGNEE_PLUGIN_DATASET env vars.
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    bump_save_counter,
    bump_turn_counter,
    hook_log,
    load_resolved,
    notify,
    resolve_user,
    touch_activity,
)
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

# Hard cap per field to avoid ballooning the cache with massive tool outputs.
_MAX_PARAMS_BYTES = 4000
_MAX_RETURN_BYTES = 8000
_MAX_ASSISTANT_BYTES = 8000


async def _fire_improve_background(dataset: str, session_id: str, user, reason: str) -> None:
    """Fire-and-forget improve() — intentionally detached from the caller.

    Prefers POST /api/v1/improve against a running backend (avoids the
    Kuzu single-writer lock). Falls back to the SDK path with
    ``run_in_background=True`` when no backend is reachable. Failures are
    logged but never raised.
    """
    from _plugin_common import improve_via_http  # type: ignore

    try:
        if improve_via_http(dataset, session_id, run_in_background=True):
            hook_log("auto_improve_fired", {"reason": reason, "session": session_id, "via": "http"})
            notify(f"auto-improve fired via HTTP ({reason})")
            return
    except Exception as exc:
        hook_log("auto_improve_http_error", {"reason": reason, "error": str(exc)[:200]})

    import cognee

    try:
        await cognee.improve(
            dataset=dataset,
            session_ids=[session_id],
            user=user,
            run_in_background=True,
        )
        hook_log("auto_improve_fired", {"reason": reason, "session": session_id, "via": "sdk"})
        notify(f"auto-improve fired ({reason})")
    except Exception as exc:
        hook_log("auto_improve_error", {"reason": reason, "error": str(exc)[:200]})


def _truncate_str(value, cap: int) -> str:
    """Coerce to string and cap at ``cap`` bytes (utf-8), appending ``...`` if truncated."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return text
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _infer_status(payload: dict) -> tuple[str, str]:
    """Return (status, error_message) from a PostToolUse payload."""
    # Claude Code sets tool_response.is_error=True on tool failures; also
    # check for an explicit 'error' key at the top level.
    response = payload.get("tool_response") or payload.get("tool_output") or ""
    if isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            err = response.get("error") or response.get("message") or "Tool reported an error."
            return "error", _truncate_str(err, 500)
    if isinstance(payload.get("error"), str) and payload["error"]:
        return "error", _truncate_str(payload["error"], 500)
    return "success", ""


def _load_session() -> tuple[str, str, str]:
    """Load session_id, dataset, user_id from resolved cache with fallbacks."""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


async def _store_tool_call(payload: dict) -> None:
    """Write a PostToolUse event as a TraceEntry."""
    import cognee
    from cognee.memory import TraceEntry

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input") or {}
    tool_output = payload.get("tool_output") or payload.get("tool_response") or ""

    # Suppress self-reference: any Bash call that mentions 'cognee' is
    # likely the plugin/CLI talking to itself and would recurse.
    if tool_name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command", ""))
        if "cognee" in cmd:
            hook_log("skip_self_cognee_bash", {"cmd_prefix": cmd[:80]})
            return

    status, error_message = _infer_status(payload)

    # Normalize method_params: small structured dict is ideal; fall back
    # to a truncated-string dict if we got something non-JSON-safe.
    if isinstance(tool_input, dict):
        params = {}
        for k, v in tool_input.items():
            params[k] = _truncate_str(v, _MAX_PARAMS_BYTES)
    else:
        params = {"value": _truncate_str(tool_input, _MAX_PARAMS_BYTES)}

    return_value = _truncate_str(tool_output, _MAX_RETURN_BYTES)

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"tool": tool_name})
        return

    config = load_config()
    await ensure_cognee_ready(config)
    user = await resolve_user(user_id)

    entry = TraceEntry(
        origin_function=tool_name,
        status=status,
        method_params=params,
        method_return_value=return_value,
        error_message=error_message,
        # LLM-backed feedback per step is expensive on a busy session —
        # fall back to the deterministic one-liner. Users who want the
        # LLM summary can flip this in a future config.
        generate_feedback_with_llm=False,
    )

    try:
        result = await cognee.remember(
            entry,
            dataset_name=dataset,
            session_id=session_id,
            user=user,
        )
    except Exception as exc:
        hook_log("trace_store_error", {"tool": tool_name, "error": str(exc)[:200]})
        notify(f"trace store failed ({exc})")
        return

    if result:
        hook_log(
            "trace_stored",
            {
                "tool": tool_name,
                "status": status,
                "trace_id": getattr(result, "entry_id", None),
            },
        )
        notify(f"trace stored ({tool_name}, {status})")
        bump_save_counter(session_id, "trace")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")
    else:
        hook_log("trace_store_noresult", {"tool": tool_name})


async def _store_assistant_stop(payload: dict) -> None:
    """Write a Stop-hook payload (final assistant message) as a QAEntry."""
    import cognee
    from cognee.memory import QAEntry

    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message") or "")
    if not msg or msg == "null":
        return

    msg = _truncate_str(msg, _MAX_ASSISTANT_BYTES)

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return

    config = load_config()
    await ensure_cognee_ready(config)
    user = await resolve_user(user_id)

    # Answer-only QAEntry: we don't have the matching question at Stop
    # time. Leaving question="" keeps the entry searchable by the
    # assistant message text.
    entry = QAEntry(question="", answer=msg, context="")

    try:
        result = await cognee.remember(
            entry,
            dataset_name=dataset,
            session_id=session_id,
            user=user,
        )
    except Exception as exc:
        hook_log("stop_store_error", {"error": str(exc)[:200]})
        notify(f"stop store failed ({exc})")
        return

    if result:
        hook_log("stop_stored", {"chars": len(msg), "qa_id": getattr(result, "entry_id", None)})
        notify(f"assistant message stored ({len(msg)} chars)")
        bump_save_counter(session_id, "answer")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")

        # Bump session turn_count + maybe auto-rename. Best-effort; failures
        # never block the Stop hook.
        try:
            import sessions as _S

            cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
            project_hash = _S.compute_project_hash(cwd)
            await _S.increment_turn(project_hash)
            renamed = await _S.auto_rename_if_due(project_hash)
            if renamed:
                hook_log(
                    "session_auto_renamed",
                    {"new_label": renamed.label, "session": str(renamed.id)},
                )
                notify(f"session auto-renamed → {renamed.label}")
        except Exception as exc:
            hook_log("session_post_stop_error", {"error": str(exc)[:200]})


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return

    is_stop = "--stop" in sys.argv
    try:
        if is_stop:
            asyncio.run(_store_assistant_stop(payload))
        else:
            asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
