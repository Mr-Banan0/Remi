"""Run Golden with subject decisions replayed from a completed Remi database."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from time import monotonic
from types import MethodType

from scripts import run_golden


def load_subject_replay(database: Path):
    connection = sqlite3.connect(database)
    try:
        records = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM requests")]
    finally:
        connection.close()
    owners = {record["owner"] for record in records if record["owner"].startswith("golden:")}
    if len(owners) != 1:
        raise ValueError("frozen_subject_database_requires_one_golden_owner")
    decisions = {}
    for record in records:
        for unit in record.get("units", {}).values():
            source = unit.get("shared_recall", {}).get("new_user_source", {})
            source_id = source.get("id")
            outputs = unit.get("outputs", {})
            decision = (unit.get("identity", {}).get("effective_output")
                        or outputs.get("subjects_repair") or outputs.get("subjects"))
            if not source_id or not isinstance(decision, dict):
                continue
            previous = decisions.get(source_id)
            if previous is not None and previous != decision:
                raise ValueError("frozen_subject_source_has_conflicting_decisions:" + source_id)
            decisions[source_id] = decision
    if not decisions:
        raise ValueError("frozen_subject_database_has_no_decisions")
    return next(iter(owners)), decisions


def install_subject_replay(engine, decisions):
    def maintain_subjects(model, state, *, deadline):
        started = monotonic()
        source_id = state.get("new_user_source", {}).get("id")
        try:
            if source_id not in decisions:
                raise RuntimeError("frozen_subject_decision_missing:" + str(source_id))
            return deepcopy(decisions[source_id])
        finally:
            model.calls.append({"kind": "add_subjects_frozen", "model": model.model,
                                "source_id": source_id, "seconds": monotonic() - started})
    engine.model.maintain_subjects = MethodType(maintain_subjects, engine.model)
    return engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--frozen-subject-database", type=Path, required=True)
    parser.add_argument("--allow-create-schema", action="store_true")
    args = parser.parse_args()
    owner, decisions = load_subject_replay(args.frozen_subject_database)
    token = owner.split(":", 1)[1]
    original_environment_engine = run_golden.environment_engine

    def environment_engine():
        return install_subject_replay(original_environment_engine(), decisions)

    class FrozenToken:
        hex = token

    run_golden.environment_engine = environment_engine
    run_golden.uuid4 = lambda: FrozenToken()
    execute_args = argparse.Namespace(dataset=args.dataset, baseline=None, output=args.output,
        env_file=args.env_file, execute=True, allow_create_schema=args.allow_create_schema, resume=False)
    run_golden.execute(execute_args)
    result_path = args.output / "result.json"
    result = json.loads(result_path.read_text())
    result["frozen_subjects"] = {"source_database": str(args.frozen_subject_database),
        "owner": owner, "decision_count": len(decisions), "mode": "final_subject_decision_replay"}
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
