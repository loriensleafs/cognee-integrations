---
name: project-rename
description: Rename the active cognee project. Use when the user explicitly asks to rename the project ("rename this project to X", "call this project Y"). Doesn't change the project_hash or any data — only the human-visible name used by `/cognee-memory:project-activate` matching.
---

# Rename the active cognee project

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project-cli.py rename "$ARGUMENTS"
```

Output:

```
Renamed active project → <new name>
```

## When to use

- User explicitly asks to rename the project
- The original name turned out to be unclear or mistaken

## What changes / what stays

- ✅ Display name (`name` field on Project DataPoint, `project_name` in cache)
- ❌ project_hash (still derived from project_root)
- ❌ project_root, git remote, git default branch
- ❌ Sessions, observations, relations stored under this project
