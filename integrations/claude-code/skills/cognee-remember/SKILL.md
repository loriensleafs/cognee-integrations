---
name: cognee-remember
description: Store data permanently in the Cognee knowledge graph.
---

# Cognee Permanent Memory Storage

Store data permanently in the Cognee knowledge graph.

## Instructions

Run:

```bash
COGNEE_SKIP_CONNECTION_TEST=true cognee-cli remember "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}"
```

The `COGNEE_SKIP_CONNECTION_TEST=true` prefix bypasses cognee's 30s LLM connection test, which times out against local Ollama models (cold start ~22s + structured-output coercion exceeds the hard-coded ceiling).

The command outputs a summary after completion:

```
Data ingested and knowledge graph built successfully!
  Dataset ID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
  Items processed: 1
  Content hash: a1b2c3d4...
  Elapsed: 4.2s
```

**IMPORTANT**: Do NOT use the `-b` (background) flag. Always run in the foreground to ensure the full pipeline completes.

## Category routing (LLM-side, not enforced at storage)

cognee-cli 1.0.3 does not yet expose `--node-set` on `remember`/`add` (only `search`). Until upstream parity ships, treat the categories below as routing guidance for *what to remember*, not as a stored tag:

| Category | What belongs here |
|----------|-------------------|
| user | User preferences, corrections, personal facts, communication style |
| project | Repository docs, code context, architecture decisions, company data |
| agent | Reasoning traces, conclusions, discovered patterns (routine tool logs are auto-captured by hooks) |

## When to use

- User says "remember this" or "save this" → user category
- User says "remember this about the project/codebase" → project category
- You want to persist your own findings or conclusions → agent category
- NOT for routine tool call logging (that's automatic via hooks)
