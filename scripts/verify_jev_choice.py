"""Small public synthetic Choice contract check using the configured gateway."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import monotonic

import httpx

from remi.settings import load_environment, private_key


def run(env_file, output):
    load_environment(env_file)
    cases = {
        "preference": "For every future invoice, send me an itemized PDF instead of a spreadsheet.",
        "fact": "我的办公门禁权限目前被冻结，管理员解除之前无法进入机房。",
        "event": "I calibrated the PX-42 sensor this afternoon.",
        "experience": "My lesson from the failed shipment: checking the packing seal before dispatch prevents this damage.",
    }
    labels = {
        "event": "One particular occurrence, plan or change.",
        "fact": "An established background, relationship, resource or continuing condition.",
        "preference": "An ongoing explicitly expressed choice, format preference, habit or liking.",
        "experience": "An explicitly learned lesson grounded in an observed result and its future applicability.",
        "raw_only": "No independent memory claim; generic question or one-off formatting request.",
        "uncertain": "The supplied source does not establish a clear category.",
    }
    questions = {name: {"type": "choice", "instructions": f"Classify only cases.{name} using the definitions.",
                        "criteria": labels} for name in cases}
    state = {"cases": cases, "comparison": {
        "new": "From now on, weekly release notes must be PDF; keep the security appendix.",
        "c1": "Weekly release notes are DOCX with a security appendix.",
        "c2": "Monthly sales presentations are PDF.",
        "unrelated": "For my gardening log, use numbered entries."}}
    questions.update({
        "target": {"type": "choice", "instructions": "Which comparison candidate concerns the SAME recurring item as comparison.new?",
                   "criteria": {"c1": "comparison.c1 is the same item.", "c2": "comparison.c2 is the same item.",
                                "none": "Neither candidate is the same item."}},
        "operation": {"type": "choice", "instructions": "What does comparison.new do to comparison.c1?",
                      "criteria": {"update": "Changes or confirms the same ongoing claim.", "retract": "Explicitly withdraws the claim.",
                                   "keep": "Provides no supported change or confirmation.", "uncertain": "Cannot determine."}},
        "no_match": {"type": "choice", "instructions": "Which comparison candidate concerns the SAME item as comparison.unrelated?",
                     "criteria": {"c1": "comparison.c1 is the same item.", "c2": "comparison.c2 is the same item.",
                                  "none": "Neither candidate is the same item."}},
    })
    expected = {**{name: name for name in cases}, "target": "c1", "operation": "update", "no_match": "none"}
    records = []
    with httpx.Client(base_url=os.environ["TYPESAFE_BASE_URL"].rstrip("/") + "/",
                      headers={"Authorization": "Bearer " + private_key("TYPESAFE_API_KEY", "TYPESAFE_API_KEY_FILE")},
                      timeout=20) as client:
        for order in ("forward", "reversed"):
            selected = {key: {**question, "criteria": dict(list(question["criteria"].items())[::1 if order == "forward" else -1])}
                        for key, question in questions.items()}
            started = monotonic()
            response = client.post("systemone", json={"model": os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
                                                      "state": state, "questions": selected})
            record = {"order": order, "http_status": response.status_code, "seconds": round(monotonic()-started, 3)}
            if response.status_code != 200:
                records.append(record)
                break
            body = response.json()
            checked = {}
            for key, question in selected.items():
                answer = body.get("answers", {}).get(key, {})
                probabilities = answer.get("probabilities", {})
                valid = (answer.get("type") == "choice" and answer.get("choice") in question["criteria"]
                         and set(probabilities) == set(question["criteria"])
                         and all(type(v) in (int, float) and 0 <= v <= 1 for v in probabilities.values())
                         and abs(sum(probabilities.values()) - 1) < .02
                         and type(answer.get("confidence")) in (int, float) and 0 <= answer["confidence"] <= 1)
                checked[key] = {"contract_valid": valid, "choice": answer.get("choice"),
                                "confidence": answer.get("confidence"), "probabilities": probabilities,
                                "expected": expected[key], "matches_expected": answer.get("choice") == expected[key]}
            record.update(answers=checked, usage=body.get("usage"), model=body.get("model"))
            records.append(record)
    result = {"scope": "public synthetic interface check; not a benchmark", "records": records,
              "contract_passed": len(records) == 2 and all(r.get("answers") and all(a["contract_valid"] for a in r["answers"].values()) for r in records),
              "paid_requests": len(records)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.env_file, args.output)
