# Cognee Plugin Data Model

Custom DataPoint schemas added by this fork. All are graph nodes with edges
to each other; all are queryable via `cognee.search` (with `node_set` /
`node_type` filters) and via direct Cypher through `get_graph_engine()`.

## Project

```python
class Project(DataPoint):
    name: str
    project_hash: str           # sha256(abspath(root))[:12] — identity
    project_root: str
    git_remote_url: str
    git_default_branch: str
    is_active: bool
    metadata: dict = {
        "index_fields": ["name"],
        "identity_fields": ["project_hash"],
    }
```

- Identity: `project_hash`. Same root → same UUID. Re-running
  `register_project` upserts.
- Lifecycle: never auto-created. Registered explicitly via
  `/cognee-memory:project-create`. Has no end state — only `is_active`
  toggles.
- Constraints: at most one `is_active=true` per user.

## Session

```python
class Session(DataPoint):
    label: str
    status: Literal["IN_PROGRESS", "ENDED"]
    is_active: bool
    started_at: str             # ISO 8601 UTC
    ended_at: Optional[str]
    git_branch: str
    summary_snapshot: str       # PreCompact persists; hydration reads
    auto_named: bool            # False after explicit user rename (sticky)
    turn_count: int
    project: Project            # → (Session)-[:project]->(Project) edge
    metadata: dict = {"index_fields": ["label", "summary_snapshot"]}
```

- Identity: UUID (auto). The `str(self.id)` is what gets passed as
  cognee's `session_id` filter and as `node_set=[session_id]` on
  content ingestion.
- Lifecycle: `IN_PROGRESS` → `ENDED`. Drives `is_active` separately:
  at most one active in-progress per project; ENDED sessions are
  always inactive.
- Auto-create: yes, when an active project has no in-progress session
  and SessionStart fires.

## Observation

```python
class Observation(DataPoint):
    category: str               # "decision", "constraint", "risk", ...
    content: str                # prose after [category]
    context: Optional[str]      # the (parenthetical) at the end
    note: Note                  # → (Observation)-[:note]->(Note) edge
    metadata: dict = {"index_fields": ["content"]}
```

- Identity: UUID. No `identity_fields` because the prose is free-form
  and can be edited; `ingest_basic_memory_note` deletes existing
  Observations on re-ingest to avoid duplicates.
- Each Observation's `content` is embedded individually so semantic
  search surfaces specific facts, not whole notes.

## Note

```python
class Note(DataPoint):
    name: str                   # frontmatter title
    permalink: str              # stable id; lookup key
    status: Literal["TODO", "IN_PROGRESS", "DONE"]
    git_branch: Optional[str]
    note_type: Optional[str]
    created_at_iso: Optional[str]
    updated_at_iso: Optional[str]
    observations: list[Observation]
    references: list[Note]      # → (Note)-[:references]->(Note) for inline [[wikilinks]]
    metadata: dict = {
        "index_fields": ["name", "permalink"],
        "identity_fields": ["permalink"],
    }
```

- Identity: `permalink`. Same permalink → same UUID. Re-ingesting
  upserts. Stub-on-demand: forward `[[Future]]` references create
  empty `Note(status="TODO")` immediately; the actual ingest of that
  note later updates the same UUID's fields.
- `references` are loose, untyped edges for inline `[[wikilinks]]` in
  the body (excluding those in the `## Relations` section).

## Relation (reified)

```python
class Relation(DataPoint):
    relation_type: str          # "depends_on", "implements", "blocks", ...
    inverse_type: str           # auto-derived: "required_by", ...
    source_permalink: str
    target_permalink: str
    added_at_iso: str
    satisfied_at_iso: Optional[str]    # for agent-team scheduling
    jira_link_id: Optional[str]        # for Jira issue-link sync
    blocked_reason: Optional[str]
    source: Note                # → (Relation)-[:source]->(Note)
    target: Note                # → (Relation)-[:target]->(Note)
    metadata: dict = {
        "index_fields": ["relation_type"],
        "identity_fields": ["source_permalink", "target_permalink", "relation_type"],
    }
```

- Identity: composite of (source, target, type). Re-ingesting the same
  triple upserts. Drift impossible by construction.
- Bidirectional: one Relation node answers both directions of the
  edge. `inverse_type` is the canonical inverse from `INVERSE_VERBS`
  (see `basic_memory.py`).
- Why reified instead of plain edges:
  - Stable per-relation identity for **Jira sync** (map to issue-link
    IDs via `jira_link_id`)
  - **Bidirectional consistency** by construction (one record covers
    both `depends_on` and `required_by` views; can't drift)
  - **Per-edge metadata** (`satisfied_at`, `blocked_reason`,
    `jira_link_id`) for agent-team scheduling and cross-system sync
- Inline `[[wikilinks]]` are NOT reified — they go on
  `Note.references` as plain edges.

---

## Cypher query cookbook

### Find the active session for the active project

```cypher
MATCH (s)-[:project]->(p)
WHERE s.type = 'Session' AND p.type = 'Project'
  AND s.is_active = true AND p.is_active = true
RETURN s.label, s.git_branch, s.summary_snapshot
```

### What does Note A depend on?

```cypher
MATCH (a)<-[:source]-(r)-[:target]->(t)
WHERE a.type = 'Note' AND r.type = 'Relation' AND t.type = 'Note'
  AND r.relation_type = 'depends_on' AND a.permalink = $a
RETURN t.permalink, t.name, t.status
```

### What is required by Note A? (inverse of same Relation)

```cypher
MATCH (a)<-[:target]-(r)-[:source]->(s)
WHERE a.type = 'Note' AND r.type = 'Relation' AND s.type = 'Note'
  AND r.relation_type = 'depends_on' AND a.permalink = $a
RETURN s.permalink AS requirer, r.inverse_type AS verb
```

### All decision-category observations across all notes

```cypher
MATCH (o)-[:note]->(n)
WHERE o.type = 'Observation' AND n.type = 'Note'
  AND o.category = 'decision'
RETURN o.content, o.context, n.permalink
```

### Notes that mention a wikilink target

```cypher
MATCH (a)-[:references]->(b)
WHERE a.type = 'Note' AND b.type = 'Note'
  AND b.permalink = $target
RETURN a.permalink, a.name
```

### TODO notes on a specific git branch

```cypher
MATCH (n)
WHERE n.type = 'Note' AND n.status = 'TODO'
  AND n.git_branch = $branch
RETURN n.permalink, n.name
```

### Relations pending Jira sync (no jira_link_id yet)

```cypher
MATCH (r)
WHERE r.type = 'Relation' AND r.jira_link_id IS NULL
RETURN r.source_permalink, r.relation_type, r.target_permalink
```

### Relations satisfied for agent-team scheduling

```cypher
MATCH (s)<-[:source]-(r)-[:target]->(t)
WHERE r.type = 'Relation' AND r.relation_type = 'depends_on'
  AND r.satisfied_at_iso IS NOT NULL
RETURN s.permalink AS task, t.permalink AS dependency, r.satisfied_at_iso
```

---

## NodeSet conventions

The plugin tags content ingestion with `node_set` so search/recall can
scope by:

| NodeSet | Tagged on |
|---|---|
| `<session_id>` (UUID string) | Every `cognee.remember` call from session hooks |
| `user_context` / `project_docs` / `agent_actions` | Routing categories from `/cognee-memory:cognee-remember` |
| Frontmatter tags + inline `#hashtags` | Each Observation's tag-prose `cognee.add` call during BM ingestion |

Recall by tag:

```python
results = await cognee.search(
    query_text="how do we handle authentication?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    node_type=NodeSet,
    node_name=["security"],   # any tag from frontmatter or #hashtag
)
```

---

## Hot-path caches

Two JSON files keep per-prompt hooks fast (no graph round-trip just to
identify the active project / session):

- `~/.cognee-plugin/active-project.json` — sticky active-project pointer
- `~/.cognee-plugin/projects/<project_hash>/active.json` — active session
  for that project

Source of truth is the cognee graph (`is_active=true` flag on Project /
Session). Caches are rewritten on every state change. SessionStart
reconciles cache against graph and re-promotes if there's a cwd-match
mismatch.

---

## Three storage layers (read this before reasoning about persistence)

The most common reasoning mistake when working with this code is conflating
"the cache" — there are **two** of them, and only one is what `improve` drains:

| Layer | What it holds | Persistence | Cleanup / drain |
|---|---|---|---|
| `active.json` (local file) | Active session pointer | Until manually cleared | Cleared by `end_session` (unrelated to graph sync) |
| Cognee QA cache | `remember`/`recall` entries, conversational turns | TTL (default 24h) | **Drained into permanent graph by `cognee.improve`** |
| Cognee permanent graph | `Session` / `Note` / `Observation` / `Relation` entities, content nodes, embeddings, edges | Durable | Receives `improve` output |

Key facts:

- The `Session` entity itself lives in the **permanent graph** from the
  moment it's created — `add_data_points` writes it. So Cypher queries
  for "all ENDED sessions in this project" are correct immediately after
  `end_session` returns.
- The session's **conversational content** (QA cache entries) is what
  needs the bridge. Without `cognee.improve`, those entries expire on
  TTL and never make it to the durable graph.
- `clear_cache(project_hash)` clears the **first** layer. `cognee.improve`
  drains the **second** layer. They don't interact.

`end_session(sync_to_graph=True)` (the default) runs the bridge. The
`sync_to_graph=False` escape hatch is for tests and the bulk path
(`end_sessions_bulk`); production lifecycle code should always sync.
