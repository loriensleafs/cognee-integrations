# Cognee Memory Plugin for Claude Code

Persistent memory for Claude Code, backed by cognee's knowledge graph.

This is a **fork** of the upstream `topoteretes/cognee-integrations` plugin
with a multi-session lifecycle layer, an explicit project-registration model,
and a Basic Memory note importer.

## What's different from upstream

The upstream plugin uses a single deterministic `session_id` per cwd
(`cc_<dirname>_<hash>`). All memory for a given directory is one
undifferentiated bucket forever, with no notion of explicit projects or
discrete units of work.

This fork adds:

- **Projects** as first-class registered entities — explicit, never
  auto-created. Captures project root, git remote URL, and default branch.
- **Sessions** as units of work within a project, with explicit lifecycle
  (`IN_PROGRESS` / `ENDED`), only one `is_active` per project at a time.
  Auto-created (placeholder), LLM-renamed at turn 5, switchable by
  natural-language match.
- **Sticky active-project pointer** so you can work on a project from any
  cwd. cwd-match still wins when you `cd` into a registered project.
- **Branch-aware session activation** via `FileChanged` hook on
  `.git/HEAD` — works for any branch transition (CLI, IDE, external
  shell), not just Bash-routed ones.
- **PR-merge auto-end** via `FileChanged` on `.git/refs/heads/main` plus
  `gh pr list --state merged --base main` — sessions whose `git_branch`
  was just merged auto-end.
- **Basic Memory note importer** — full-fidelity ingestion of `.md` files
  using Brain's format: frontmatter, observations (per-fact embeddings),
  reified `Relation` DataPoints (bidirectional consistency by
  construction, Jira-syncable identity, agent-team scheduling metadata),
  inline `[[wikilink]]` references, stub-on-demand for forward refs.
- **Cognee-native data model**: `Project`, `Session`, `Note`,
  `Observation`, `Relation` are real custom DataPoints in the graph,
  queryable via Cypher and via `cognee.search` with `node_set` filters.

## Install

### 1. Install Cognee

```bash
pip install cognee
```

### 2. Configure cognee

Create `~/.cognee/.env` with your provider settings. Example for AWS Bedrock:

```bash
LLM_PROVIDER="bedrock"
LLM_MODEL="bedrock/us.anthropic.claude-sonnet-4-6"
AWS_REGION_NAME="us-east-1"
AWS_PROFILE="your-bedrock-profile"

EMBEDDING_PROVIDER="fastembed"
EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS="384"

GRAPH_DATABASE_PROVIDER="neo4j"
GRAPH_DATABASE_URL="bolt://localhost:7687"
GRAPH_DATABASE_USERNAME="neo4j"
GRAPH_DATABASE_PASSWORD="..."

ENABLE_BACKEND_ACCESS_CONTROL="false"
COGNEE_SKIP_CONNECTION_TEST=true
CACHING=true
```

For OpenAI / Ollama / Cognee Cloud, swap the `LLM_*` block accordingly.

### 3. Enable the plugin

```bash
claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code
```

Or alias it permanently:

```bash
# ~/.zshrc
alias claude="claude --plugin-dir ~/Dev/cognee-integrations/integrations/claude-code"
```

When the plugin loads, you'll see one of two SessionStart messages depending on whether the cwd has a registered project:

- **No project registered for this cwd** → "⚠ No active project — run `/cognee-memory:project-create`"
- **Project resolved** → "## Active project — \<name\> (root: ...)" + active session label + persisted summary snapshot if present

## Quickstart for a new project

```text
You: /cognee-memory:project-create
Claude: What would you like to name this project?
You:    Cognee Plugin Multi-Session
Claude: [runs project-cli.py create "Cognee Plugin Multi-Session"]
        Registered project **Cognee Plugin Multi-Session**
          hash:    34ba43b6b414
          root:    /Users/.../cognee-integrations
          remote:  https://github.com/.../cognee-integrations.git
          default: main
          status:  ACTIVE
        Active session: **untitled-5d00c61c** (id=9d307899)
```

After registration, every prompt is auto-stored under the active session, every tool call is auto-traced, and recall fires on every prompt.

## Slash commands

### Project lifecycle

| Command | Behavior |
|---|---|
| `/cognee-memory:project-create` | Skill asks user for a project name; CLI captures cwd + git metadata; marks active; auto-creates session |
| `/cognee-memory:project-list` | Show all registered projects, mark active |
| `/cognee-memory:project-activate <natural language>` | Fuzzy-match by name, demote prior, auto-resolve session within (1 in_progress → activate; 0 → create; 2+ → list candidates) |
| `/cognee-memory:project-rename <new name>` | Rename active project |

### Session lifecycle (within active project)

| Command | Behavior |
|---|---|
| `/cognee-memory:session-list` | Show in-progress sessions, mark active |
| `/cognee-memory:session-activate <natural language>` | Fuzzy-match in-progress session, switch active, hydrate conversation with summary_snapshot |
| `/cognee-memory:session-rename <new name>` | Rename active session, lock `auto_named=False` |
| `/cognee-memory:session-end` | End active session (or by id) — sets status=ENDED, clears active pointer |

### Memory ops (existing)

| Command | Behavior |
|---|---|
| `/cognee-memory:cognee-remember` | Permanently store data in the knowledge graph |
| `/cognee-memory:cognee-search` | Search session or graph memory, optional `--node-set` filter |
| `/cognee-memory:cognee-sync` | Force session→graph sync |

### Basic Memory importer (this fork)

| Command | Behavior |
|---|---|
| `/cognee-memory:bm-import <path>` | Ingest a `.md` file or directory of files; full Basic Memory fidelity |

## How resolution works

Every project-scoped hook (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `PreCompact`, `FileChanged`) calls one resolver: `sessions.resolve_active_project_for_hooks(cwd)`.

```text
1. cwd → Project registered at sha256(abspath(cwd))[:12]?
   → Use it. SessionStart promotes to active so the sticky cache
     reflects what every other hook will see.
2. Else, sticky active-project pointer (~/.cognee-plugin/active-project.json)?
   → Use it. (Covers parent-dir / random cwd cases.)
3. Else, cognee Project with is_active=true (recovery path)?
   → Re-cache + use it.
4. Else, None.
   → SessionStart prints "register a project" message. Memory hooks
     run in best-effort no-op mode until a project is registered.
```

cwd is the strongest signal — `cd`-ing into a registered project overrides any prior sticky pointer. The sticky pointer covers the case where you're in a parent directory and want to keep working on the project you set last time.

## Data model

| Type | Identity | Purpose |
|---|---|---|
| `Project` | `project_hash = sha256(abspath(root))[:12]` | Container; never auto-created; user-named |
| `Session` | UUID | Unit of work; `is_active`, `status`, `git_branch`, `summary_snapshot` |
| `Observation` | UUID | One per `[cat] prose` line in a Note; content embedded individually |
| `Relation` | `(source_permalink, target_permalink, relation_type)` | Reified — bidirectional consistency by construction; carries `inverse_type`, `jira_link_id`, `satisfied_at` for sync + scheduling |
| `Note` | `permalink` | Basic Memory note; stub-on-demand for forward refs |

See `references/schema.md` for full field listing and Cypher query patterns.

## Hook reference

| Hook | What it does |
|---|---|
| **SessionStart** | Resolves active project (cwd → sticky → recovery), promotes sticky cache, ensures active session, hydrates conversation with project + session summary |
| **UserPromptSubmit** | Searches memory for context relevant to the prompt; injects as `additionalContext` |
| **PostToolUse** | Captures tool input/output as `TraceEntry`; tagged with active session_id via `node_set` |
| **Stop** | Captures final assistant response as `QAEntry`; bumps session turn_count; fires LLM auto-rename at turn 5 if still `auto_named` |
| **PreCompact** | Builds memory anchor from session + trace + graph; persists as active session's `summary_snapshot` so the next SessionStart can hydrate from it |
| **SessionEnd** | Bridges session cache → permanent graph via `cognee.improve()` |
| **FileChanged** (`.git/HEAD$\|.git/refs/heads/main$`) | On HEAD change: resolve active session for new branch (auto-create / activate / list candidates); on main advance: end sessions whose git_branch was just merged into main |

## Configuration reference

Same as upstream plus a few additions:

| Key | Env var | Default | Description |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `claude_sessions` (legacy fallback only) | Dataset for permanent storage. New projects use `project_<hash>` automatically. |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` (legacy fallback) | Project-aware resolution overrides this when a project is registered |
| `service_url` | `COGNEE_SERVICE_URL` | -- | Cognee Cloud URL |
| `api_key` | `COGNEE_API_KEY` | -- | Cognee Cloud API key |
| `llm_api_key` | `LLM_API_KEY` | -- | LLM provider key (local mode) |
| `llm_model` | `LLM_MODEL` | -- | LLM model name (local mode) |

## Files

```
integrations/claude-code/
├── README.md                          this file
├── hooks/hooks.json                   FileChanged hook for git state added
├── scripts/
│   ├── sessions.py                    Project + Session DataPoints, resolver
│   ├── basic_memory.py                Note + Observation + Relation + parser
│   ├── session-cli.py                 list/end/rename/activate session commands
│   ├── project-cli.py                 list/create/activate/rename project commands
│   ├── bm-cli.py                      ingest/ingest-dir/show BM commands
│   ├── session-start.py               new resolver-driven flow
│   ├── store-user-prompt.py           tags ingest with node_set=[session_id]
│   ├── store-to-session.py            tags + bumps turn_count + auto-rename
│   ├── pre-compact.py                 persists summary_snapshot
│   ├── post-git-state-change.py       branch-aware session resolution + PR-merge auto-end
│   └── (plus unchanged upstream scripts)
├── skills/
│   ├── project-list / -create / -activate / -rename
│   ├── session-list / -end / -rename / -activate
│   └── bm-import
└── references/
    └── schema.md                      data model reference
```
