---
name: session-rename
description: Rename the active session. Use when the user explicitly asks to rename the session, when the LLM-generated placeholder label needs replacement, or when the user provides a clearer name for the work in progress. User-set names are sticky — they will not be overwritten by the auto-rename pass.
---

# Rename the active session

Run with the new label as the argument:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/session-cli.py rename "$ARGUMENTS"
```

Output:

```
Renamed active session → <new label>
```

Renaming the active session sets `auto_named=False`. Once a user has
explicitly chosen a label, the LLM auto-naming pass leaves it alone
forever — even if the session content drifts.

## When to use

- User explicitly says "rename this session to X" / "call this session X"
- User provides a name when they realize the placeholder `untitled-XXX`
  is still in place
- User wants to fix a poor auto-generated label

## Best label form

Short, action-oriented, 4–7 words. Examples:

- "Multi-session plugin refactor"
- "Auth middleware compliance fix"
- "OPS-1421 webhook retry investigation"

Avoid: vague labels ("misc work"), dates ("April session"), or
commit-message-shaped labels ("feat: add X").
