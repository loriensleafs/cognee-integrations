---
name: session-end
description: End the active session (or a specific session by short id). Use when the user explicitly says "end this session", "wrap up this session", "this session is done", or has finished a discrete unit of work and wants it filed away. Ended sessions become read-only and stop receiving memory writes.
---

# End the active session

Run, with no arguments, to end the currently-active session:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-cli.py end
```

To end a specific session by id (full UUID or first 8 chars):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-cli.py end "$ARGUMENTS"
```

Output:

```
Ended session <short-id> — <label>
```

After ending, the session's status flips to `ENDED` and it is removed
from the in-progress set. The active pointer for the project is
cleared if the ended session was active. Subsequent memory ops will
auto-create a fresh session on the next user prompt.

## When to use

- User explicitly says "end this session" / "session done" / "we're done with this"
- A discrete piece of work has shipped and the user wants it filed
- After PR merge if the user wants closure (the FileChanged hook may
  auto-end branch-bound sessions on main-advance; this is the manual path)

## When NOT to use

- Don't end on conversation close — sessions span conversations
- Don't end on branch switch — the auto-rules handle branch transitions
