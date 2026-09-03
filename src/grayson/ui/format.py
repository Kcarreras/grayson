"""Console presentation helpers: agent-text sectioning, glossary, graph model.

Agents return long continuous prose. Where it carries deterministic markers
(WHY THIS FIX:, RISKS:, ROLLOUT:, ...), split_sections() turns it into labeled
blocks the templates render as visually distinct chunks instead of a wall of
text. The glossary feeds the inline help widgets that orient new users.
relationship_graph() turns the knowledge library into the node/edge model the
relationship canvas draws; the layout itself is ELK's, in the browser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from markupsafe import Markup, escape

# ALL-CAPS heading followed by a colon, e.g. "WHY THIS FIX:", "RISKS:",
# "ASSUMPTIONS TO CONFIRM BEFORE APPLYING:". Requires >= 3 chars so ordinary
# sentences with a capitalized word and colon ("SQL:") rarely false-positive.
_SECTION_RE = re.compile(r"(?:(?<=^)|(?<=[.?!)\]]\s)|(?<=\n))([A-Z][A-Z0-9 /&',-]{2,60}):\s+")


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


#: what each base-descriptor gap is called on a tile, where "3 gaps" says
#: nothing a reader can act on. Keyed by the item `completeness()` reports.
_GAP_LABELS = {
    "grain": "no grain",
    "columns": "no columns",
    "freshness": "no freshness",
    "relationships": "no relationships",
    "definition_files": "no definition",
}
_COLUMN_GAP = re.compile(r"^column_descriptions \((\d+)/(\d+)\)$")


def gap_label(item: str) -> str:
    """A completeness gap as a reader names it: `grain` -> "no grain",
    `column_descriptions (3/10)` -> "3/10 columns described"."""
    m = _COLUMN_GAP.match(item)
    if m:
        return f"{m.group(1)}/{m.group(2)} columns described"
    return _GAP_LABELS.get(item, item.replace("_", " "))


def paragraphs(text: object, sentences_per_para: int = 3) -> Markup:
    """Break agent prose into readable paragraphs.

    Existing blank-line breaks are respected; a wall of text with none is
    split deterministically every few sentences. Escaped first — this renders
    agent-authored content. Findings' `extra` values are arbitrary JSON, so
    non-string values render sensibly instead of crashing: a list becomes one
    paragraph per item, a dict a key: value line, anything else its str().
    """
    if isinstance(text, list | tuple):
        items = [str(x).strip() for x in text if str(x).strip()]
        return Markup("".join(f"<p>{escape(i)}</p>" for i in items))
    if isinstance(text, dict):
        text = "; ".join(f"{k}: {v}" for k, v in text.items())
    elif not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.strip()
    if not text:
        return Markup("")
    if "\n\n" in text:
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    else:
        sentences = _SENTENCE_RE.split(text)
        paras = [
            " ".join(sentences[i : i + sentences_per_para]).strip()
            for i in range(0, len(sentences), sentences_per_para)
        ]
    return Markup("".join(f"<p>{escape(p)}</p>" for p in paras if p))


def split_sections(text: str) -> list[dict]:
    """Split agent prose into [{heading, body}]; heading None for the preamble."""
    if not text:
        return []
    sections: list[dict] = []
    last_end = 0
    heading: str | None = None
    for m in _SECTION_RE.finditer(text):
        body = text[last_end : m.start()].strip()
        if body or heading is not None:
            sections.append({"heading": heading, "body": body})
        heading = m.group(1).strip().title()
        last_end = m.end()
    tail = text[last_end:].strip()
    if tail or heading is not None:
        sections.append({"heading": heading, "body": tail})
    return sections or [{"heading": None, "body": text.strip()}]


#: Plain-language explanations for the inline help widgets. Keep each under
#: ~35 words; the widget is orientation, not documentation.
GLOSSARY: dict[str, str] = {
    "guard": (
        "Every statement an agent submits is parsed and checked before it runs. "
        "Only read statements (SELECT, SHOW, DESCRIBE, EXPLAIN) survive — "
        "writes and schema changes are blocked outright."
    ),
    "guard_profile": (
        "A named bundle of guard limits: automatic row caps, query timeouts, and "
        "query budgets. 'strict' is tight, 'generous' is loose. Set per session."
    ),
    "strict_scope": (
        "When on, reading rows of a table outside the session's scope is blocked "
        "instead of warned about. Names, columns, and DDL stay readable and are "
        "recorded, so the agent can ask for a table by name."
    ),
    "scope": (
        "Tables readable beside the targets: named at setup, brought in as library "
        "views, granted on a scope request, or added here. Widening is always a "
        "user action and lands in the event log."
    ),
    "target_tables": (
        "The tables this session is investigating. Evidence must actually touch "
        "them — unrelated queries can't be cited to close checkpoints."
    ),
    "checkpoints": (
        "The workflow's required steps. Each closes only by citing queries that "
        "really executed and touched the target tables — and, where the workflow "
        "asks for one, a chart of the required kind built from a cited query — or is "
        "waived by you, with a reason, when it does not apply."
    ),
    "evidence": (
        "Ids of queries that actually ran (q_0001, ...). Checkpoints, findings, "
        "and verifications must cite them; claims without evidence are rejected."
    ),
    "interventions": (
        "Questions the agent asks you when judgment is needed — labeling samples, "
        "confirming semantics, choosing between fixes. It waits for your answer."
    ),
    "findings": (
        "Structured problem reports validated against the workflow's schema. "
        "You accept the real ones (fixes only begin after an accepted finding) "
        "or reject them with a reason the agent works from. Findings are never "
        "edited: a corrected finding proposes to supersede the old one, and the "
        "swap executes only when you accept it — the history stays visible."
    ),
    "severity": (
        "How bad, on a shared scale — critical: wrong data is already being used "
        "for decisions; high: real and material, but bounded or not yet consumed; "
        "medium: genuine, limited blast radius; low: nobody is currently harmed; "
        "info: not a defect, just worth knowing. grayson does not judge whether the "
        "agent got it right — that is what accepting or rejecting is for — but it "
        "does make the top rungs cost specificity: 'critical' and 'high' must name "
        "the objects affected, and 'high' confidence must come with a reproduction."
    ),
    "proposals": (
        "Concrete fixes the agent drafts — a file diff or DDL for you to apply. "
        "Agents never get write access; you approve and apply."
    ),
    "verification": (
        "Deterministic before/after proof a fix worked: the anomaly query re-run "
        "after applying, compared to the original, both cited as evidence."
    ),
    "stage": (
        "Where the session is in its lifecycle: setup, analysis, synthesis, "
        "review, fixes, verification, closed. The first executed query moves "
        "setup to analysis automatically; later stages are declared by the "
        "agent as it works. Gates enforce the boundaries that matter: review "
        "is blocked until required checkpoints are complete, and fixes until "
        "you accept a finding — agents cannot force past either."
    ),
    "purpose": (
        "The label the agent attached when running the query — its stated "
        "reason, kept in the audit log. Columns with no values are hidden."
    ),
    "knowledge": (
        "The durable, team-shareable record of what tables mean: grain, column "
        "definitions, relationships, and confirmed facts. Agents read it at "
        "session start and add to it as they learn."
    ),
    "base_descriptor": (
        "The structured minimum for a fully described table: grain, column "
        "definitions, relationships, freshness, and definition files. "
        "'base complete' means none are missing."
    ),
    "definitions": (
        "Where the table is actually defined — the dbt model, the view's SELECT, "
        "the DDL. An entry is a pointer into the repo that owns it plus a hash and "
        "date; a captured copy beside the doc is a dated snapshot, never the "
        "authority."
    ),
    "fact_status": (
        "Provenance of a fact: proposed (agent asserted), data_inferred (derived "
        "from queries), user_confirmed (a human blessed it — agents can never "
        "set this themselves)."
    ),
    "external_checks": (
        "Deterministic checks your automation (Airflow, dbt, ...) runs on a "
        "schedule, dropped into the library as JSON. Agents see failing checks "
        "on their target tables at session start as pre-vetted leads."
    ),
    "workflows": (
        "Core workflows ship with grayson and are read-only. Team workflows live "
        "in the shared library's workflows/ folder and travel by git; only their "
        "author edits one — anyone else forks a copy under their own id."
    ),
    "workflow_yaml": (
        "The YAML is the definition. Saving validates it — parse, shape, findings "
        "schema, checkpoint keys, ownership — and writes it to the team library, "
        "where it travels on push."
    ),
    "session_flow": (
        "Setup inputs, then analysis, then findings, then fixes. Every gate "
        "between stages is enforced by grayson's code, not by prompting the agent."
    ),
    "suggested_checks": (
        "Checks the workflow expects an agent to consider where they apply. None "
        "blocks a stage; those the agent takes up become ordinary checkpoints."
    ),
    "findings_schema": (
        "The shape every finding must validate against, plus at least one "
        "executed query as evidence. Extra required keys are the workflow's own."
    ),
    "setup_inputs": (
        "What the human supplied when the session started. Checkpoints that "
        "declare an input work from it."
    ),
    "records": (
        "Every finding, proposal, and session report across all sessions — yours "
        "and, through the library, the team's. Agents search the same index."
    ),
    "open_questions": (
        "Questions an agent left for a human. Answering one retires it and "
        "records the answer as a user-confirmed fact for future sessions."
    ),
    "settings": (
        "Stored in grayson.toml, committed with the workspace. Change it here or "
        "with the grayson config command; agents can read it, never change it."
    ),
    "workflow_defaults": (
        "Defaults for new sessions of each workflow. A flag at session start "
        "still wins; 'inherit' falls back to the workflow's own suggestion."
    ),
    "library_admins": (
        "Admins may remove any published record; everyone else removes only "
        "their own. The list lives in the library's library.toml."
    ),
    "charts": (
        "Visuals the agent builds from cached query results as it works — each "
        "chart is tied to an executed query id, so every picture is traceable "
        "evidence, refreshed live as the analysis progresses."
    ),
}

STAGES_ORDER = ["setup", "analysis", "synthesis", "review", "fixes", "verification", "closed"]


# ---- relationship graph ------------------------------------------------
#
# The console ships a Cytoscape.js canvas laid out by ELK (both vendored under
# ui/static/vendor, so the console still works offline). Python's job is only to
# turn the knowledge library's per-table `relationships` lists into one
# deduplicated node/edge model; layout is ELK's, and it is the layout that keeps
# nodes and edge routes from colliding at any schema size.

#: Guard rail for the library-wide map. ELK is comfortable with a few hundred
#: nodes; past that both the browser and the reader stop coping, so we truncate
#: to the best-connected slice and say so in the UI.
MAX_GRAPH_NODES = 240


def _leaf(fqn: str) -> str:
    return fqn.split(".")[-1]


def _qualifier(fqn: str) -> str:
    """DB.SCHEMA for a three-part name — the grouping key and disambiguator."""
    return ".".join(fqn.split(".")[:-1])


def _pair_key(a: str, b: str, on: str) -> tuple[str, str, str]:
    """Direction-independent identity of a relationship.

    A declaring 'to B on ID' and B declaring 'to A on ID' describe one edge, not
    two. The old diagram drew both, which is where its duplicated parallel lines
    came from.
    """
    lo, hi = sorted((a, b))
    return (lo, hi, re.sub(r"\s+", "", str(on).upper()))


def _collect_edges(docs: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """Every declared relationship across `docs`, deduplicated."""
    seen: dict[tuple[str, str, str], dict] = {}
    for source, doc in docs.items():
        for rel in doc.get("relationships") or []:
            if not isinstance(rel, Mapping):  # store.read normalizes; stay safe anyway
                continue
            target = str(rel.get("to") or "").strip().upper()
            if not target or target == source:
                continue
            on = str(rel.get("on") or "")
            key = _pair_key(source, target, on)
            if key in seen:
                # Keep the first declaration but remember that both sides agree;
                # a relationship only one side knows about is worth flagging.
                seen[key]["mutual"] = True
                continue
            seen[key] = {
                "source": source,
                "target": target,
                "on": on,
                "cardinality": str(rel.get("cardinality") or ""),
                "note": str(rel.get("note") or ""),
                "mutual": False,
            }
    edges = list(seen.values())
    for i, edge in enumerate(edges):
        edge["id"] = f"e{i}"
    return edges


def relationship_graph(
    docs: Mapping[str, Mapping[str, Any]],
    focus: str | None = None,
    max_nodes: int = MAX_GRAPH_NODES,
) -> dict | None:
    """Node/edge model for the relationship canvas, or None if there is nothing to draw.

    With `focus`, the model is that table's neighbourhood: the table, everything
    directly related to it in either direction, and the relationships those
    neighbours have with each other — the local ERD, not just the spokes.
    Without `focus`, it is the whole library.
    """
    edges = _collect_edges(docs)
    if not edges:
        return None

    if focus:
        focus = focus.upper()
        neighbours = {focus}
        for e in edges:
            if e["source"] == focus:
                neighbours.add(e["target"])
            elif e["target"] == focus:
                neighbours.add(e["source"])
        edges = [e for e in edges if e["source"] in neighbours and e["target"] in neighbours]
        if not edges:
            return None
        keep = neighbours
    else:
        keep = {end for e in edges for end in (e["source"], e["target"])}

    truncated = 0
    if len(keep) > max_nodes:
        # Drop the least-connected tables first: they are the ones a reader can
        # look up individually, and they are what makes a big map unreadable.
        degree: dict[str, int] = dict.fromkeys(keep, 0)
        for e in edges:
            degree[e["source"]] += 1
            degree[e["target"]] += 1
        ranked = sorted(keep, key=lambda t: (-degree[t], t))
        truncated = len(keep) - max_nodes
        keep = set(ranked[:max_nodes]) | ({focus} if focus else set())
        edges = [e for e in edges if e["source"] in keep and e["target"] in keep]

    nodes = [
        {
            "id": fqn,
            "label": _leaf(fqn),
            "qualifier": _qualifier(fqn),
            # Referenced but never described: worth showing as a gap in the map
            # rather than silently drawing it like a documented table.
            "known": fqn in docs,
            "focus": fqn == focus,
        }
        for fqn in sorted(keep)
    ]
    return {
        "focus": focus,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        # Labelling every edge is legible for a neighbourhood and noise for a
        # whole library; the canvas exposes a toggle either way.
        "edge_labels": len(edges) <= 24,
    }
