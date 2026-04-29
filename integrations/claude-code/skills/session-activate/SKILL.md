---
name: session-activate
description: Activate an in-progress session by natural-language description and hydrate the conversation with its summary. Use when the user wants to switch to a different in-progress session ("switch me to the auth refactor session", "go back to the multi-session plugin work", "let's pick up where I left off on X"). Deactivates the currently-active session before activating the target.
---

# Activate an in-progress session

Run with a natural-language description of the session to activate:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-cli.py activate "$ARGUMENTS"
```

The description is matched semantically against in-progress sessions'
labels and persisted summaries. Single match → activated. Ambiguous
matches → printed list, user must be more specific. No match → the
in-progress catalogue is shown so they can pick.

Single-match output:

```
Session **<label>** is now active (id=<short>).

## Where this session left off

<persisted summary_snapshot>
```

The summary is the persisted "where this session left off" payload
written by PreCompact and other state-snapshot hooks. It serves as
the hydration context for the now-active session — the agent should
read it as if it were a CLAUDE.md preamble specific to the session's
work.

## When to use

- User explicitly says "switch to the X session" / "activate the Y work"
- User describes prior work and wants to resume ("pick up the auth
  refactor", "go back to that ticket I was working on")
- After `/session-list` to switch to a specific session

## Lifecycle

Activation:

1. Locates the target via natural-language search (top match in the
   `IN_PROGRESS` set for this project).
2. Removes `ACTIVE` nodeset from the previously-active session (it
   stays `IN_PROGRESS` — paused, not ended).
3. Adds `ACTIVE` nodeset to the target.
4. Updates the active.json cache so per-prompt hooks pick up the
   change immediately.
5. Emits the target session's `summary_snapshot` to the conversation.

The previously-active session is **paused**, not ended. It can be
re-activated later with another `session-activate` call.
