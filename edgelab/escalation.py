"""Read-only handoff for cases the local workflow cannot resolve."""

from .contracts import fingerprint


def escalation_for(verified: dict) -> dict | None:
    report, bundle = verified["report"], verified["bundle"]
    if report["status"] == "reconciliation required":
        action = "Reconcile the reserved tool call without replaying it."
    elif report["verdict"] == "revise":
        action = "Draft a new protocol and obtain approval of its exact digest."
    elif report["verdict"] == "unresolved":
        action = "Review the recorded evidence and decide whether to stop or propose a new protocol."
    else:
        return None
    packet = {"run_id": verified["run_id"], "ledger_sha256": bundle["ledger_sha256"],
              "status": report["status"], "verdict": report["verdict"], "reason": report["reason"],
              "evidence_refs": report["evidence_refs"], "action": action, "holdout": report["holdout"]}
    return {"ticket_sha256": fingerprint(packet), **packet}
