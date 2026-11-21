"""Read-only reporting over controller-recorded evidence."""

from dataclasses import asdict

from .contracts import fingerprint
from .journal import Journal, JournalConflict
from .messages import ToolBinding, check_version
from .methods import BY_ID, CORPUS_SHA256
from .research import ResearchService, validate_id
from .stat_tools import TOOL_SEQUENCE, Window


def report_from_journal(journal: Journal, run_id: str) -> dict:
    events = journal.events(validate_id(run_id))
    by_key = {event["key"]: event["value"] for event in events}
    plan = by_key.get("plan")
    if not plan or plan.get("run_id") != run_id:
        raise JournalConflict("No matching EdgeLab plan")
    approval = by_key.get("approval")
    verdict = by_key.get("verdict")
    if approval and (approval.get("plan_sha256") != fingerprint(plan) or
                     not any(item == {key: approval.get(key) for key in ("digest", "protocol")}
                             for item in plan["candidates"]) or
                     approval["digest"] != fingerprint(approval["protocol"])):
        raise JournalConflict("Report cannot verify the saved approval")
    if approval and "version" in approval["protocol"]:
        evidence = plan.get("method_evidence", [])
        if (any(not isinstance(note, dict) or not isinstance(note.get("id"), str)
                or note["id"] not in BY_ID or asdict(BY_ID[note["id"]]) != note for note in evidence)
                or sorted(note["id"] for note in evidence) != approval["protocol"].get("evidence_ids")
                or approval["protocol"].get("corpus_sha256") != CORPUS_SHA256):
            raise JournalConflict("Report cannot verify the approved method citations")
    findings, refs = [], []
    reserved_only = []
    expected_order = ["plan"] + (["approval"] if approval else [])
    allowed = approval is not None
    for tool in TOOL_SEQUENCE:
        reserved = by_key.get(f"reserved:{tool}")
        result_id = f"result:{tool}"
        result = by_key.get(result_id)
        finding = by_key.get(f"review:{tool}")
        if approval:
            protocol = approval["protocol"]
            expected = {"approval": approval["digest"], "tool": tool, "window": protocol["window"],
                        "sample_sha256": approval["sample_sha256"]}
            if "version" in protocol:
                try:
                    check_version(protocol["version"])
                    expected = asdict(ToolBinding(run_id, approval["digest"], tool,
                                                  Window(**protocol["window"]), approval["sample_sha256"]))
                    if protocol["run_id"] != run_id:
                        raise ValueError
                except (KeyError, TypeError, ValueError) as error:
                    raise JournalConflict("Report cannot verify the versioned run binding") from error
        else:
            expected = None
        if reserved is not None and reserved != expected:
            raise JournalConflict("Report cannot verify the saved reservation")
        if reserved is not None and result is None:
            reserved_only.append(tool)
        if result is not None:
            if (expected is None or reserved != expected or result.get("id") != result_id or
                    any(result.get(key) != value for key, value in expected.items())):
                raise JournalConflict("Report cannot verify a tool result binding")
        step_keys = [key for key in (f"reserved:{tool}", result_id, f"review:{tool}") if key in by_key]
        if step_keys and not allowed:
            raise JournalConflict("Report found a tool outside the approved milestone sequence")
        expected_order.extend(step_keys)
        if finding is not None:
            if (result is None or finding.get("result_id") != result_id or
                    ("evidence_sha256" in finding and finding["evidence_sha256"] != fingerprint(result))):
                raise JournalConflict("Report cannot verify a Critic evidence reference")
            if approval and "version" in approval["protocol"] and finding != ResearchService.review_result(
                    tool, approval, result):
                raise JournalConflict("Report finding does not match the verified observation")
            findings.append(finding)
            refs.append(result_id)
        allowed = allowed and finding is not None and finding.get("decision") == "proceed"
    if verdict is not None and (verdict.get("evidence_refs") != refs or
                                (approval and (reserved_only or not findings or
                                               verdict.get("verdict") not in {"continue", "revise", "stop", "unresolved"}
                                               or verdict.get("verdict") != findings[-1]["decision"]
                                               or verdict.get("reason") != findings[-1]["reason"])) or
                                (not approval and (plan["status"] != "unresolved"
                                                   or verdict.get("verdict") != "unresolved"
                                                   or verdict.get("reason") != plan.get("reason")))):
        raise JournalConflict("Report cannot verify the recorded verdict against the evidence")
    if list(by_key) != expected_order + (["verdict"] if verdict else []):
        raise JournalConflict("Report found an invalid ledger event order")
    missing = ["holdout evaluation"]
    if not approval:
        missing.append("approved protocol")
    if not verdict:
        missing.append("controller verdict")
    missing.extend(f"reconciliation for {tool}" for tool in reserved_only)
    history = plan["input"].get("history")
    return {
        "run_id": run_id,
        "report": {
            "status": "finished" if verdict else "reconciliation required" if reserved_only
            else "approved" if approval else plan["status"],
            "verdict": verdict["verdict"] if verdict else None,
            "reason": verdict["reason"] if verdict else plan.get("reason"),
            "evidence_refs": refs, "milestone_reviews": findings, "scope": "in-sample research only",
            "holdout": "locked and untested", "retrieval": plan["retrieval"],
            "source": history["source"] if history else "EdgeLab fixed-seed synthetic fixture",
            "window": plan["input"]["window"],
        },
        "bundle": {"events": events, "ledger_sha256": fingerprint(events), "missing": missing,
                   "raw_series_included": False},
    }
