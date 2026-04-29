---
name: project-activate
description: Switch the active cognee project by natural-language name match. Use when the user says "switch to the X project", "activate the Y project", "let's work on Z", or wants memory operations to route to a different registered project than the cwd implies. The active project pointer is sticky across Claude Code conversations — once set, it persists until explicitly changed. Within the activated project, the active session is auto-resolved (single in-progress → activate; none → create; multiple → list candidates).
---

# Activate a registered cognee project

Run with a natural-language description:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project-cli.py activate "$ARGUMENTS"
```

The query is substring-matched (case-insensitive) against project
names. Single match → activated. Ambiguous → list shown for the user
to refine. No match → list of all registered projects shown so they
can pick.

Single-match output:

```
Project **<name>** is now active (root: `<root>`)

Active session: **<session label>** (id=<short>)

## Where this session left off
<persisted summary_snapshot>
```

## Session auto-resolution rules within the activated project

| In-progress sessions | Behavior |
|---|---|
| 1 already marked `is_active=true` | Use it directly |
| 1 in-progress, not active | Promote it to active |
| 0 | Create `untitled-XXXX` placeholder, mark active |
| 2+ in-progress, none active | Don't pick automatically; list candidates and tell user to run `/cognee-memory:session-activate <description>` |

## When to use

- User explicitly says "switch to the X project" / "activate the Y project"
- User describes prior work on a different project ("let's pick up the
  auth refactor that was in cognee-integrations")
- User wants to override the cwd-derived project (working from a
  parent directory or unrelated path)

## When NOT to use

- For switching sessions within the current project — use
  `/cognee-memory:session-activate` instead
- To register a brand-new project — use `/cognee-memory:project-create`
