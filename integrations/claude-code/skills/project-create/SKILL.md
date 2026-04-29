---
name: project-create
description: Register a new cognee project. **Always ask the user what they want to name the project before invoking this skill** — the project name is user-visible and is what `/cognee-memory:project-activate` matches against. Captures the project root path, the git origin remote URL, and the git default branch automatically. Use when the user says "register this project", "create a project", "set up cognee for this repo", or when SessionStart printed "no active project" and the user wants to fix it.
---

# Register a new cognee project

## STEP 1 — ask the user for the project name (REQUIRED)

Don't guess. Don't derive from directory name. Use AskUserQuestion or
plain prompt to ask the user something like:

> What would you like to name this project? (This is the human-readable
> label you'll use to switch between projects.)

Wait for the user's answer. The name they provide is what gets stored.

## STEP 2 — confirm the project root (usually cwd)

If unsure or the user hasn't said, the project root defaults to the
current working directory. If the user wants to register a different
path, ask them which path. Pass the path as the second argument.

## STEP 3 — invoke the CLI

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project-cli.py create "<name from step 1>" [<path from step 2>]
```

Example:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project-cli.py create "Cognee Integrations Plugin"
```

## What gets captured

- **name** — what the user gave you (case-preserved, free-form)
- **project_hash** — `sha256(absolute_root)[:12]`, the deterministic identity key
- **project_root** — absolute path
- **git_remote_url** — `git remote get-url origin` (if the path is a git repo)
- **git_default_branch** — detected via `git symbolic-ref refs/remotes/origin/HEAD`, falling back to `main` / `master`
- **is_active** — set to true; demotes any prior active project

## After registration

The newly-registered project is marked active, and a placeholder
session (`untitled-XXXX`) is auto-created within it. The CLI output
shows both. The session can be renamed later via
`/cognee-memory:session-rename`, or it'll auto-rename via the LLM
after 5 turns of conversation.
