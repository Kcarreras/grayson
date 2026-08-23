"""Console presentation helpers: agent-text sectioning, glossary, graph layout.

Agents return long continuous prose. Where it carries deterministic markers
(WHY THIS FIX:, RISKS:, ROLLOUT:, ...), split_sections() turns it into labeled
blocks the templates render as visually distinct chunks instead of a wall of
text. The glossary feeds the inline help widgets that orient new users.
"""

from __future__ import annotations

import math
import re

# ALL-CAPS heading followed by a colon, e.g. "WHY THIS FIX:", "RISKS:",
# "ASSUMPTIONS TO CONFIRM BEFORE APPLYING:". Requires >= 3 chars so ordinary
# sentences with a capitalized word and colon ("SQL:") rarely false-positive.
_SECTION_RE = re.compile(r"(?:(?<=^)|(?<=[.?!)\]]\s)|(?<=\n))([A-Z][A-Z0-9 /&',-]{2,60}):\s+")


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
        "When on, queries touching tables outside the session's declared targets "
        "are blocked instead of just warned about."
    ),
    "target_tables": (
        "The tables this session is investigating. Evidence must actually touch "
        "them — unrelated queries can't be cited to close checkpoints."
    ),
    "checkpoints": (
        "The workflow's required steps. Each can only be closed by citing queries "
        "that really executed — the agent cannot claim unverifiable work."
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
        "You accept the real ones; fixes can only begin after an accepted finding."
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
        "review, fixes, verification, closed. Gates block review until "
        "checkpoints are complete, and fixes until a finding is accepted."
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
    "fact_status": (
        "Provenance of a fact: proposed (agent asserted), data_inferred (derived "
        "from queries), user_confirmed (a human blessed it — agents can never "
        "set this themselves)."
    ),
}

STAGES_ORDER = ["setup", "analysis", "synthesis", "review", "fixes", "verification", "closed"]


def relationship_graph(center: str, relationships: list[dict], width: int = 640) -> dict | None:
    """Hub-and-spoke layout for a table's relationships, rendered as inline SVG.

    Returns node/edge coordinates, or None when there is nothing to draw.
    """
    rels = [r for r in relationships if r.get("to")]
    if not rels:
        return None
    height = max(220, 90 + 62 * ((len(rels) + 1) // 2))
    cx, cy = width / 2, height / 2
    radius = min(cx - 130, cy - 40)
    nodes, edges = [], []
    for i, rel in enumerate(rels):
        angle = (2 * math.pi * i / len(rels)) - math.pi / 2
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        label = str(rel["to"]).split(".")[-1]
        nodes.append({"x": x, "y": y, "label": label, "full": str(rel["to"])})
        edges.append(
            {
                "x1": cx,
                "y1": cy,
                "x2": x,
                "y2": y,
                "mx": (cx + x) / 2,
                "my": (cy + y) / 2 - 6,
                "label": str(rel.get("on", "")),
                "cardinality": str(rel.get("cardinality", "")),
            }
        )
    return {
        "width": width,
        "height": height,
        "cx": cx,
        "cy": cy,
        "center_label": center.split(".")[-1],
        "nodes": nodes,
        "edges": edges,
    }
