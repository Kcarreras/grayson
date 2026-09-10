"""Display priorities for hands-on QA; execution gates remain in the engine."""


def review_order(findings):
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return sorted(
        findings,
        key=lambda f: (
            bool(f.get("accepted") or f.get("rejected") or f.get("superseded_by")),
            severity.get(f.get("severity"), 5),
        ),
    )


def focus(summary, ready, interventions, proposals):
    if summary["stage"] == "closed":
        return None
    decisions = []
    questions = sum(i["status"] == "open" for i in interventions)
    pending = len(ready["findings_pending"])
    proposed = sum(p["status"] == "proposed" and not p.get("superseded_by") for p in proposals)
    approved = sum(p["status"] == "approved" and not p.get("superseded_by") for p in proposals)
    failed = sum(
        p["status"] == "verification_failed" and not p.get("superseded_by") for p in proposals
    )

    def proposal_target(status):
        return next(
            (
                "proposal-" + p["pid"]
                for p in proposals
                if p["status"] == status and not p.get("superseded_by")
            ),
            "proposals",
        )

    for count, label, target in (
        (questions, "questions to answer", "awaiting"),
        (pending, "findings to review", "findings"),
        (proposed, "fix proposals to review", proposal_target("proposed")),
        (approved, "approved fixes awaiting application", proposal_target("approved")),
        (failed, "fixes with failed verification", proposal_target("verification_failed")),
    ):
        if count:
            if count == 1:
                label = (
                    label.replace("questions", "question")
                    .replace("findings", "finding")
                    .replace("proposals", "proposal")
                    .replace("fixes", "fix")
                )
            decisions.append({"count": count, "label": label, "target": target})
    if decisions:
        return {"title": "Needs your attention", "items": decisions}
    if ready["clean_close_available"]:
        return None  # The existing clean-close card already presents this decision.
    return {
        "title": "Investigation in progress",
        "message": (
            f"{len(ready['open_checks'])} required checkpoints still need evidence."
            if ready["open_checks"]
            else "Required checkpoints are clear. Continue through the workflow's "
            "review and verification stages."
        ),
        "items": [],
        "target": "checkpoints" if ready["open_checks"] else "actions",
    }
