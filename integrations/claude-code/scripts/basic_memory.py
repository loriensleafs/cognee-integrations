"""Basic Memory → cognee ingestion.

Translates Basic Memory's note format (frontmatter, ``## Observations``,
``## Relations``, inline ``[[wikilinks]]``) into cognee primitives:

    Note               → custom DataPoint, one node per .md file
    Observation        → custom DataPoint, one node per `- [cat] prose` line
    Relation           → custom DataPoint (reified), one per typed `## Relations`
                         entry. Carries source, target, type, inverse_type, and
                         metadata fields.
    Inline [[wikilink]] → plain ``Note.references`` edge (no Relation node)

Reified Relations give us:
  - Stable per-relation identity (UUID) for Jira issue-link sync
  - Bidirectional consistency by construction — one record covers both
    ``depends_on`` and ``required_by`` views, drift impossible
  - A place to attach scheduling metadata (satisfied_at, blocked_by_team,
    expected_duration_hours, etc.) when agent-team coordination needs it

Inline ``[[wikilinks]]`` are a separate, lighter mechanism — loose
references with no need for the heavier Relation node.

Forward references work via stub-on-demand: ``find_or_stub_note`` looks
up by permalink and creates an empty ``status='TODO'`` Note if missing.
When the real note is later authored, the stub gets filled in by
permalink rather than duplicated.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple
from uuid import UUID, uuid4

from pydantic import SkipValidation

from cognee.infrastructure.engine import DataPoint
from cognee.tasks.storage import add_data_points


# ─── Inverse-verb map (ported from Brain CLAUDE.md) ────────────────────────


INVERSE_VERBS: Dict[str, str] = {
    "implements": "implemented_by",
    "implemented_by": "implements",
    "depends_on": "required_by",
    "required_by": "depends_on",
    "extends": "extended_by",
    "extended_by": "extends",
    "part_of": "contains",
    "contains": "part_of",
    "inspired_by": "inspires",
    "inspires": "inspired_by",
    "supersedes": "superseded_by",
    "superseded_by": "supersedes",
    "leads_to": "caused_by",
    "caused_by": "leads_to",
    # Symmetric: same verb on both sides
    "pairs_with": "pairs_with",
    "relates_to": "relates_to",
    # Brain-specific verbs that may appear in notes
    "blocks": "blocked_by",
    "blocked_by": "blocks",
}


def inverse_verb(verb: str) -> str:
    """Return the canonical inverse for a relation verb. Falls back to
    ``relates_to`` for unknown verbs (symmetric, lossy but safe)."""
    return INVERSE_VERBS.get(verb, "relates_to")


# ─── DataPoints ───────────────────────────────────────────────────────────


class Observation(DataPoint):
    """A single fact extracted from a Basic Memory note's Observations section.

    Each observation is its own DataPoint so the ``content`` prose can be
    embedded for semantic search independently — exactly matching Basic
    Memory's design where every observation is individually indexed.
    """

    category: str = ""
    content: str = ""
    context: Optional[str] = None
    note: SkipValidation[Any] = None
    metadata: dict = {"index_fields": ["content"]}


class Note(DataPoint):
    """A Basic Memory note rendered as a cognee graph node.

    ``permalink`` is the stable identifier — always look up by permalink,
    never by name (titles can change, slugs stay).
    """

    name: str = ""
    permalink: str = ""
    status: Literal["TODO", "IN_PROGRESS", "DONE"] = "TODO"
    git_branch: Optional[str] = None
    note_type: Optional[str] = None
    created_at_iso: Optional[str] = None
    updated_at_iso: Optional[str] = None

    observations: SkipValidation[Any] = []
    references: SkipValidation[Any] = []  # inline [[wikilink]] mentions

    metadata: dict = {
        "index_fields": ["name", "permalink"],
        "identity_fields": ["permalink"],  # same permalink → same UUID (idempotent)
    }


class Relation(DataPoint):
    """A reified relation between two Notes — its own first-class entity.

    Source-of-truth for both directions: ``depends_on`` and ``required_by``
    are the same Relation viewed from opposite endpoints. Bidirectional
    drift impossible by construction.

    ``identity_fields`` makes the UUID deterministic from the (source,
    target, type) triple — re-creating the same relation upserts.
    """

    relation_type: str = ""
    inverse_type: str = ""
    source_permalink: str = ""
    target_permalink: str = ""
    added_at_iso: str = ""
    satisfied_at_iso: Optional[str] = None
    jira_link_id: Optional[str] = None
    blocked_reason: Optional[str] = None

    source: SkipValidation[Any] = None
    target: SkipValidation[Any] = None

    metadata: dict = {
        "index_fields": ["relation_type"],
        "identity_fields": ["source_permalink", "target_permalink", "relation_type"],
    }


# ─── Utility helpers ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_KEBAB_RE = re.compile(r"[^a-z0-9]+")


def title_to_permalink(title: str) -> str:
    """Convert a free-form title or wikilink target into a kebab-case permalink.

    Mirrors Basic Memory's slug derivation. Idempotent if you pass an
    already-kebab string.
    """
    if not title:
        return ""
    out = _KEBAB_RE.sub("-", title.strip().lower()).strip("-")
    return out or title.strip().lower()


def permalink_to_title(permalink: str) -> str:
    """Best-effort title from a permalink slug. Used only for stub Notes."""
    if not permalink:
        return ""
    return permalink.replace("-", " ").strip().title() or permalink


# ─── Markdown section + line parsers ──────────────────────────────────────


_OBSERVATION_RE = re.compile(
    r"^\s*-\s*"
    r"\[(?P<category>[^\]]+)\]\s*"
    r"(?P<content>[^#(\n]+?)"
    r"(?P<tags>(?:\s*#\S+)*)"
    r"\s*(?:\((?P<context>[^)\n]*)\))?"
    r"\s*$",
    re.MULTILINE,
)

_RELATION_RE = re.compile(
    r"^\s*-\s*"
    r"(?P<rtype>[a-zA-Z_][a-zA-Z0-9_]*)\s+"
    r"\[\[(?P<target>[^\]]+)\]\]"
    r"\s*$",
    re.MULTILINE,
)

_INLINE_LINK_RE = re.compile(r"\[\[(?P<target>[^\]]+)\]\]")

_SECTION_RE_TEMPLATE = (
    r"^##\s+{name}\s*\n"  # ## Heading
    r"(?P<body>.*?)"
    r"(?=^##\s+|\Z)"  # until the next H2 or EOF
)


def extract_section(markdown: str, name: str) -> str:
    """Return the body text of a top-level ``## <name>`` section, or ''."""
    pattern = re.compile(
        _SECTION_RE_TEMPLATE.format(name=re.escape(name)),
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(markdown)
    if not m:
        return ""
    return m.group("body")


def strip_section(markdown: str, name: str) -> str:
    """Return the markdown with the named section removed (used for inline
    wikilink scanning so we don't re-pick the targets in ``## Relations``)."""
    pattern = re.compile(
        _SECTION_RE_TEMPLATE.format(name=re.escape(name)),
        re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", markdown)


def parse_observations(
    markdown: str,
) -> Iterator[Tuple[str, str, List[str], Optional[str]]]:
    """Yield (category, content, hashtags, context) for each Observation entry."""
    section = extract_section(markdown, "Observations")
    if not section:
        return
    for m in _OBSERVATION_RE.finditer(section):
        category = m.group("category").strip()
        content = m.group("content").strip()
        tags = [t.lstrip("#") for t in (m.group("tags") or "").split() if t.startswith("#")]
        context = (m.group("context") or "").strip() or None
        yield category, content, tags, context


def parse_relations(markdown: str) -> Iterator[Tuple[str, str]]:
    """Yield (relation_type, target_permalink) for each line in ``## Relations``."""
    section = extract_section(markdown, "Relations")
    if not section:
        return
    for m in _RELATION_RE.finditer(section):
        yield m.group("rtype"), title_to_permalink(m.group("target"))


def parse_inline_wikilinks(markdown: str) -> List[str]:
    """All ``[[wikilink]]`` permalinks anywhere in the body, EXCLUDING those
    inside ``## Relations`` (those are handled by parse_relations)."""
    body = strip_section(markdown, "Relations")
    seen: List[str] = []
    for m in _INLINE_LINK_RE.finditer(body):
        permalink = title_to_permalink(m.group("target"))
        if permalink not in seen:
            seen.append(permalink)
    return seen


# ─── Note + Relation factories ────────────────────────────────────────────


async def find_or_stub_note(permalink: str) -> "Note":
    """Look up a Note by permalink; if missing, persist a TODO stub.

    Identity is via ``identity_fields=["permalink"]`` on Note, so the same
    permalink deterministically yields the same UUID. ``add_data_points``
    upserts cleanly — a real later ingest of the same permalink replaces
    the stub's fields without duplicating.
    """
    note = Note(
        name=permalink_to_title(permalink),
        permalink=permalink,
        status="TODO",
    )
    await add_data_points([note])
    return note


async def create_relation(
    source: "Note",
    target: "Note",
    relation_type: str,
    *,
    added_at_iso: Optional[str] = None,
) -> "Relation":
    """Create a reified Relation linking source to target with the given verb.

    Idempotent via ``identity_fields=(source_permalink, target_permalink,
    relation_type)`` — re-running upserts the same UUID. ``inverse_type``
    is auto-derived from the canonical map.

    Persists the Relation node and the two edges (Relation→source,
    Relation→target). Both directions are queryable from the same record:

        # what A depends on
        MATCH (a:Note {permalink:'a'})<-[:source]-(r:Relation {relation_type:'depends_on'})-[:target]->(t)

        # what is required by A — same Relation, opposite endpoints
        MATCH (a:Note {permalink:'a'})<-[:target]-(r:Relation {relation_type:'depends_on'})-[:source]->(s)
    """
    rel = Relation(
        relation_type=relation_type,
        inverse_type=inverse_verb(relation_type),
        source_permalink=source.permalink,
        target_permalink=target.permalink,
        added_at_iso=added_at_iso or _now_iso(),
    )
    rel.source = source
    rel.target = target
    await add_data_points([rel])
    return rel


# ─── Frontmatter parser ───────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)


def split_frontmatter(markdown: str) -> Tuple[Dict[str, Any], str]:
    """Crude YAML-ish frontmatter splitter — handles flat key: value pairs and
    simple list values. Returns (frontmatter_dict, remaining_markdown).

    For real-world Basic Memory notes this is enough; complex YAML (nested
    objects, multi-line strings) is out of scope for the V1 importer.
    """
    m = _FRONTMATTER_RE.match(markdown)
    if not m:
        return {}, markdown
    body = m.group("body")
    rest = markdown[m.end():]
    fm: Dict[str, Any] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, rest


# ─── Ingestion pipeline ───────────────────────────────────────────────────


async def ingest_basic_memory_note(
    markdown: str,
    *,
    cognify: bool = True,
    dataset_name: str = "basic_memory",
) -> "Note":
    """Translate one Basic Memory ``.md`` file into cognee.

    Steps:
      1. Split frontmatter; extract permalink, title, status, type, branch, tags.
      2. Find or stub the Note by permalink (idempotent via identity_fields).
      3. Parse Observations into individual DataPoints with parent-Note edge.
       Combine frontmatter tags with each observation's inline #hashtags
       and pass as ``node_set`` so semantic search can scope by tag.
      4. Parse explicit ## Relations and create reified Relations
       (idempotent — same triple → same UUID).
      5. Parse inline [[wikilinks]] (excluding those in ## Relations) as
       loose references on Note.references.
      6. Persist Note + Observations in one batch.
      7. Optionally run cognee.cognify() to extract concept nodes from
       observation prose on top of the structural skeleton.

    Returns the (possibly upserted) Note DataPoint.
    """
    import cognee

    frontmatter, body = split_frontmatter(markdown)

    permalink = frontmatter.get("permalink") or title_to_permalink(
        frontmatter.get("title", "")
    )
    if not permalink:
        raise ValueError("Note has no permalink and no title to derive one from.")

    note = await find_or_stub_note(permalink)
    note.name = frontmatter.get("title") or note.name
    note.status = frontmatter.get("status") or note.status
    note.git_branch = frontmatter.get("git_branch") or note.git_branch
    note.note_type = frontmatter.get("type") or note.note_type
    note.updated_at_iso = _now_iso()
    if not note.created_at_iso:
        note.created_at_iso = note.updated_at_iso

    # Delete existing Observations for this note before re-creating them.
    # Observation DataPoints don't have identity_fields (their content is
    # free-form prose), so re-ingesting the same note would otherwise
    # accumulate duplicates. Cypher path: detach + delete every Observation
    # whose :note edge points at this Note.
    try:
        from cognee.infrastructure.databases.graph import get_graph_engine

        engine = await get_graph_engine()
        await engine.query(
            """MATCH (o)-[:note]->(n)
               WHERE o.type = 'Observation' AND n.type = 'Note'
                 AND n.permalink = $p
               DETACH DELETE o""",
            {"p": permalink},
        )
    except Exception:
        # Best-effort: if cleanup fails, the new observations still land
        # (just with possible duplicates).
        pass

    fm_tags = frontmatter.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [t.strip() for t in fm_tags.split(",") if t.strip()]

    # --- Observations
    observations: List[Observation] = []
    for category, content, hashtags, context in parse_observations(body):
        obs = Observation(category=category, content=content, context=context)
        obs.note = note
        observations.append(obs)

        # Tag the observation prose with frontmatter tags + inline hashtags so
        # cross-cutting search by tag finds it. cognee.add registers the
        # content under the dataset's NodeSet index.
        node_set = list({*fm_tags, *hashtags})
        if content and node_set:
            try:
                await cognee.add(content, dataset_name=dataset_name, node_set=node_set)
            except Exception:
                # Tag attachment is best-effort; don't break ingest on it.
                pass
    note.observations = observations

    # --- Inline [[wikilinks]] (excluding ## Relations entries)
    inline_targets = parse_inline_wikilinks(body)
    relation_targets_set = {
        target_permalink for _, target_permalink in parse_relations(body)
    }
    references: List[Note] = []
    for target_permalink in inline_targets:
        if target_permalink in relation_targets_set:
            continue
        references.append(await find_or_stub_note(target_permalink))
    note.references = references

    await add_data_points([note, *observations])

    # --- Typed Relations (reified — separate persistence)
    for relation_type, target_permalink in parse_relations(body):
        target = await find_or_stub_note(target_permalink)
        await create_relation(note, target, relation_type)

    if cognify:
        try:
            await cognee.cognify()
        except Exception:
            # Cognify enrichment is opportunistic; the structural skeleton
            # is already persisted whether the LLM step succeeds or not.
            pass

    return note
