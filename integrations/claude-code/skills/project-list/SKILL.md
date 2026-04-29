---
name: project-list
description: List all registered cognee projects, with the active one marked. Use when the user asks "what projects do I have", "show registered projects", or wants to see the catalogue before activating one. Projects are explicit — registered via /cognee-memory:project-create — they're never created automatically.
---

# List registered cognee projects

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project-cli.py list
```

Output:

```
## Registered projects

  <project_hash>  <name> [ACTIVE]
           root: /path/to/project · default-branch:main · <git remote URL>
  <project_hash>  <other-name>
           root: /path/to/other · default-branch:main
```

Active project is listed first and marked `[ACTIVE]`. Memory hooks
(remember, recall, store-prompt, store-trace) all route to the active
project's dataset.

If no projects are registered yet, output is:

```
No projects registered yet.
Run /cognee-memory:project-create to register one.
```

## When to use

- User asks "what projects exist" / "list projects"
- Before `project-activate` to see what's available
- To verify which project is currently active
