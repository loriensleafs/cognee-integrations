#!/usr/bin/env python3
"""CLI surface for Basic Memory note ingestion.

Subcommands:

    ingest <file>           — ingest one .md file
    ingest-dir <directory>  — recursively ingest every .md under <directory>
    show <permalink>        — print a Note's frontmatter, observations, and relations

Each subcommand initializes cognee + agent identity once before running.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, os.path.dirname(__file__))
from config import ensure_cognee_ready, ensure_identity, load_config
import basic_memory as BM


async def _bootstrap() -> None:
    config = load_config()
    await ensure_cognee_ready(config)
    await ensure_identity(config)


async def cmd_ingest(args: List[str]) -> int:
    if not args:
        print("Usage: bm-cli ingest <file.md>", file=sys.stderr)
        return 2
    path = Path(args[0]).expanduser().resolve()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2
    await _bootstrap()
    try:
        markdown = path.read_text(encoding="utf-8")
        note = await BM.ingest_basic_memory_note(markdown)
        n_obs = len(note.observations or [])
        n_refs = len(note.references or [])
        print(
            f"Ingested {path.name} → permalink={note.permalink} "
            f"(obs={n_obs}, refs={n_refs})"
        )
        return 0
    except Exception as exc:
        print(f"Failed to ingest {path}: {exc}", file=sys.stderr)
        return 1


async def cmd_ingest_dir(args: List[str]) -> int:
    if not args:
        print("Usage: bm-cli ingest-dir <directory>", file=sys.stderr)
        return 2
    root = Path(args[0]).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    await _bootstrap()
    files = sorted(root.rglob("*.md"))
    if not files:
        print(f"No .md files under {root}")
        return 0
    print(f"Found {len(files)} .md file(s) under {root}")
    ok = 0
    fail = 0
    for path in files:
        try:
            markdown = path.read_text(encoding="utf-8")
            note = await BM.ingest_basic_memory_note(markdown)
            ok += 1
            n_obs = len(note.observations or [])
            print(f"  ✓ {path.relative_to(root)} → {note.permalink} ({n_obs} obs)")
        except Exception as exc:
            fail += 1
            print(f"  ✗ {path.relative_to(root)}: {exc}", file=sys.stderr)
    print(f"\nDone: {ok} ingested, {fail} failed.")
    return 0 if fail == 0 else 1


async def cmd_show(args: List[str]) -> int:
    if not args:
        print("Usage: bm-cli show <permalink>", file=sys.stderr)
        return 2
    permalink = args[0]
    await _bootstrap()

    from cognee.infrastructure.databases.graph import get_graph_engine

    engine = await get_graph_engine()

    note_rows = await engine.query(
        """MATCH (n) WHERE n.type = 'Note' AND n.permalink = $p
           RETURN properties(n) AS props""",
        {"p": permalink},
    )
    if not note_rows:
        print(f"No Note found for permalink: {permalink}")
        return 1
    props = note_rows[0].get("props") if isinstance(note_rows[0], dict) else note_rows[0][0]
    print(f"## {props.get('name', permalink)}")
    print(f"permalink: {permalink}")
    print(f"status:    {props.get('status', '?')}")
    if props.get("git_branch"):
        print(f"branch:    {props['git_branch']}")
    if props.get("note_type"):
        print(f"type:      {props['note_type']}")

    obs_rows = await engine.query(
        """MATCH (o)-[:note]->(n)
           WHERE o.type = 'Observation' AND n.type = 'Note' AND n.permalink = $p
           RETURN o.category AS category, o.content AS content, o.context AS context""",
        {"p": permalink},
    )
    if obs_rows:
        print("\n### Observations")
        for r in obs_rows:
            cat = r.get("category", "?")
            content = r.get("content", "")
            ctx = r.get("context")
            ctx_str = f" ({ctx})" if ctx else ""
            print(f"  - [{cat}] {content}{ctx_str}")

    rel_out = await engine.query(
        """MATCH (n)<-[:source]-(r)-[:target]->(t)
           WHERE n.type = 'Note' AND r.type = 'Relation' AND n.permalink = $p
           RETURN r.relation_type AS verb, t.permalink AS target, t.name AS name""",
        {"p": permalink},
    )
    rel_in = await engine.query(
        """MATCH (n)<-[:target]-(r)-[:source]->(s)
           WHERE n.type = 'Note' AND r.type = 'Relation' AND n.permalink = $p
           RETURN r.inverse_type AS verb, s.permalink AS source, s.name AS name""",
        {"p": permalink},
    )
    if rel_out or rel_in:
        print("\n### Relations")
        for r in rel_out or []:
            print(f"  → {r.get('verb', '?')} [[{r.get('name') or r.get('target')}]]")
        for r in rel_in or []:
            print(f"  ← {r.get('verb', '?')} [[{r.get('name') or r.get('source')}]]")

    return 0


_COMMANDS = {
    "ingest": cmd_ingest,
    "ingest-dir": cmd_ingest_dir,
    "show": cmd_show,
}


async def _main(argv: List[str]) -> int:
    if not argv or argv[0] not in _COMMANDS:
        print(f"Usage: bm-cli {{{'|'.join(_COMMANDS)}}} [args...]", file=sys.stderr)
        return 2
    return await _COMMANDS[argv[0]](argv[1:])


def main() -> int:
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
