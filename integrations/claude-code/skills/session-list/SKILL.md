---
name: session-list
description: List all sessions (active + in-progress) for the current project. Use when the user asks "what sessions are open?", "what was I working on?", or wants to see the catalogue of in-progress work in this project before deciding what to do next.
---

# List sessions for this project

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-cli.py list
```

Output is a markdown report:

```
## Sessions for <project-name> (project_<hash>)

### In progress
  <short-id>  <label> [ACTIVE] · branch:<branch>
           started <iso-timestamp> · turns <n>
```

The `[ACTIVE]` flag marks the currently-active session — the one
memory operations (remember / recall / store) are routed to.

If no in-progress sessions exist yet, output is:

```
(no sessions yet — one will be created on your next prompt)
```

## When to use

- User asks "what sessions are open" / "what's in progress"
- Before `session-activate` to know which session names exist
- To verify which session is currently active
