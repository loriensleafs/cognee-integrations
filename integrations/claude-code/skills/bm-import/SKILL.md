---
name: bm-import
description: Ingest Basic Memory note(s) into the cognee knowledge graph with full fidelity — frontmatter, observations (per-fact embeddings), reified Relations (bidirectional consistency), and inline wikilink references. Use when the user says "ingest this note", "import this Basic Memory file", "load my Brain notes into cognee", or provides a path to a `.md` file or a directory of them. Stub-creation handles forward references — `[[Future Note]]` references become TODO stubs that get filled in when the real note is later authored.
---

# Import Basic Memory notes into cognee

## Single file

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bm-cli.py ingest "$ARGUMENTS"
```

Output:

```
Ingested <filename> → permalink=<slug> (obs=N, refs=M)
```

## Whole directory (recursive)

If the argument is a directory rather than a file, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bm-cli.py ingest-dir "$ARGUMENTS"
```

Output:

```
Found N .md file(s) under <directory>
  ✓ path/to/note-a.md → note-a-permalink (3 obs)
  ✓ path/to/note-b.md → note-b-permalink (5 obs)
  ...
Done: N ingested, 0 failed.
```

## Inspect an ingested note

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/bm-cli.py show "<permalink>"
```

Prints the note's frontmatter, observations, and outbound + inbound relations with arrows showing direction.

## What gets stored

| Source markdown | Cognee primitive |
|---|---|
| Frontmatter `title` / `permalink` / `status` / `git_branch` / `type` | Fields on `Note` DataPoint |
| Frontmatter `tags` | NodeSet for cross-cutting filtering |
| `## Observations` block, `- [cat] prose #tag (context)` | One `Observation` DataPoint per line, content embedded for semantic search |
| `## Relations` block, `- verb [[Target]]` | Reified `Relation` DataPoint with stable UUID, source + target edges, auto-derived `inverse_type` for bidirectional queries |
| Inline `[[wikilink]]` in body | Plain `Note.references` edge (lighter than typed Relations) |
| `[[Future Note]]` referenced before file exists | Stub `Note(status='TODO')` created on demand; filled in later |

## Idempotency

- **Notes**: `permalink` is the identity key. Re-ingesting the same file by permalink updates fields in place, no duplicates.
- **Observations**: deleted and re-created on every ingest (no stable identity for free-form prose).
- **Relations**: identity is `(source_permalink, target_permalink, relation_type)`. Re-ingesting the same triple is an upsert.
- **Stubs**: same permalink → same UUID, so the actual ingest of a previously-stubbed note replaces fields without duplicating the node.

## When to use

- User wants to seed cognee from existing Brain notes (`/Users/peter.kloss/...`)
- User has a fresh `.md` file they want to add
- User wants inspection / verification of a stored note (`show`)

## When NOT to use

- For arbitrary text without Basic Memory structure → use `/cognee-memory:cognee-remember` instead
- For session memory (auto-stored prompts/answers) → that's automatic via the hook plugin; nothing to do
