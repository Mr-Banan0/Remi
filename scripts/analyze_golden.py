"""Read completed evidence-retrieval reports and explain omissions without model calls."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def analyze(report):
    mapping = report["source_to_ref"]
    details = []
    for result in report["questions"]:
        missing = [ref for ref, rank in result["evidence_ranks"].items() if rank is None]
        if not missing:
            continue
        candidates = result["trace"]["candidates"]
        parts = []
        for ref in missing:
            pool = [item for item in candidates if any(mapping.get(key) == ref for key in item["source_ids"])]
            selected = [item for item in pool if item["selected"]]
            channels = [channel["channel"] for channel in result["trace"].get("candidate_channels", [])
                        if any(mapping.get(key) == ref for key in channel["source_ids"])]
            shown_to_decision = any(mapping.get(key) == ref for key in result["trace"].get("decision_source_ids", []))
            stage = ("selection_or_original_packaging" if selected else "ranking_selection" if pool else
                     "candidate_allocation" if channels else "not_in_final_candidate_pool")
            parts.append({"ref": ref, "stage": stage, "candidate_count": len(pool),
                "candidate_rerank_scores": [item.get("rerank_score") for item in pool],
                "selected_count": len(selected), "recall_channels": channels, "shown_to_decision": shown_to_decision})
        details.append({"question_id": result["question_id"], "question": result["question"],
            "recall": result["evidence_recall"], "missing": parts, "decision": result["trace"]["decision"],
            "tool_results": result["trace"]["tool_results"], "decision_errors": result["trace"].get("decision_errors", []),
            "hot_conditions": result["trace"]["hot_conditions"], "selected_conditions": result["trace"]["selected_conditions"]})
    derivation = report.get("derivation", {})
    units = [unit for group in derivation.values() for request in group["requests"] for unit in request["units"].values()]
    commands = [command for unit in units for command in unit.get("maintain", {}).get("edits", unit.get("commands", []))]
    disposition = Counter(item["error"] for unit in units for item in unit.get("rejected", []))
    validations = [entry for unit in units for item in unit.get("validation", [])
                   for entry in (item.get("validation", []) if item.get("pipeline") else [item])]
    source_calls = report["diagnostic_calls"]
    kinds = sorted({call["kind"] for call in source_calls})
    memories = derivation.get("rhelm", {}).get("memories", [])
    return {"complete": report["complete"], "scope": "retrieval evidence diagnosis; no final-answer judge",
        "bad_cases": details, "lifecycle_failures": [item["query"] for item in report["lifecycle_searches"]
                                                      if not item["top1_expected_evidence"]],
        "memory_kinds": dict(Counter(tag for memory in memories for tag in memory["revisions"][-1].get("tags", [memory.get("kind", "unknown")]))),
        "commands": dict(Counter(command["operation"] for command in commands)),
        "commands_by_kind": dict(Counter(tag for command in commands for tag in command.get("tags", [command.get("kind", "unknown")]))),
        "dispositions": dict(disposition),
        "validation_failures_by_stage": dict(Counter(item["stage"] + "|" + item["error"]
            for item in validations if item.get("error"))),
        "validation_failures_by_kind": dict(Counter(item.get("kind", "unclassified_prepare") + "|" + item["error"]
            for item in validations if item.get("error"))),
        "unavailable_condition_selection_count": sum(result["trace"].get("unavailable_condition_selection_count", 0)
            for result in report["questions"]),
        "unknown_event_times": sum("event" in memory["revisions"][-1].get("tags", [memory.get("kind")]) and memory["revisions"][-1]["event_at"] is None for memory in memories),
        "physical_call_seconds": {kind: {"count": sum(call["kind"] == kind for call in source_calls),
            "total_seconds": sum(call["seconds"] for call in source_calls if call["kind"] == kind)} for kind in kinds},
        "tool_coverages": dict(Counter(tool["coverage"] for result in report["questions"]
                                       for tool in result["trace"]["tool_results"])),
        "decision_error_count": sum(len(result["trace"].get("decision_errors", [])) for result in report["questions"]),
        "average_returned_bytes": sum(result["returned_bytes"] for result in report["questions"]) / len(report["questions"])
            if report["questions"] else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("fresh_output_required")
    report = json.loads(args.result.read_text())
    args.output.write_text(json.dumps(analyze(report), ensure_ascii=False, indent=2))
    print(json.dumps({"analysis": str(args.output), "paid_calls": 0}))


if __name__ == "__main__":
    main()
