"""Route original text, split content when needed, and persist fixed typed decisions."""
from __future__ import annotations

from time import monotonic

from pydantic import ValidationError

from .models import ContentItem, WrittenItem
from .providers import OutputInvalid, ProviderUnavailable


PIPELINE = "G07-memory-boundaries"
TIME_ERRORS = {"time_unresolved_choose_supported_expression_or_null",
               "end_time_unresolved_choose_supported_expression_or_null", "validity_interval_inverted"}


class AddPipeline:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _split_entries(plan):
        raws = plan.get("items") if isinstance(plan, dict) and set(plan) == {"items"} else None
        if not isinstance(raws, list) or not 2 <= len(raws) <= 24:
            return [], [{"position": -1, "error": "split_list_invalid"}]
        entries, failures, seen = [], [], set()
        for position, raw in enumerate(raws):
            try:
                item = ContentItem.model_validate(raw)
                signature = item.content.strip()
                if signature in seen:
                    failures.append({"position": position, "error": "split_duplicate_content"})
                    continue
                seen.add(signature)
                entries.append({"item_id": f"i{position}", "new_item": item.model_dump()})
            except ValidationError:
                failures.append({"position": position, "error": "split_content_schema_invalid"})
        return entries, failures

    @staticmethod
    def command(fields, focused, operation, target):
        """Original source and context bindings are owned exclusively by the program."""
        from .engine import source_quote
        primary = focused["new_user_source"]
        quote = source_quote(primary["content"], focused["new_item"]["content"]) or primary["content"]
        return {**fields, "operation": operation, "target_id": target["id"] if target else None,
                "kind": focused["kind"], "source_id": primary["id"], "quote": quote,
                "context_source_ids": [source["id"] for source in focused["context_sources"]]}

    def _write_plan(self, plan, edits):
        """Validate items independently; valid siblings survive one failed writer result."""
        raws = plan.get("items") if isinstance(plan, dict) and set(plan) == {"items"} else None
        if not isinstance(raws, list) or len(raws) > 24:
            return {}, [{"item_id": edit["item_id"], "error": "written_list_invalid"} for edit in edits]
        by_id = {edit["item_id"]: edit for edit in edits}
        valid, failed, seen = {}, [], set()
        for raw in raws:
            key = raw.get("item_id") if isinstance(raw, dict) else None
            if not isinstance(key, str) or key not in by_id or key in seen:
                failed.append({"item_id": None, "error": "written_item_id_invalid"})
                continue
            seen.add(key)
            edit = by_id[key]
            try:
                fields = WrittenItem.model_validate(raw).model_dump(exclude={"item_id"})
                command = self.command(fields, edit, edit["operation"], edit["target"])
                checked = self.engine._validate_command(command, edit["comparison_state"])
                valid[key] = checked.model_dump()
            except (ValueError, ValidationError) as error:
                failed.append({"item_id": key, "error": "written_content_schema_invalid"
                               if isinstance(error, ValidationError) else str(error)[:200], "fields": raw})
        failed.extend({"item_id": key, "error": "written_item_missing"} for key in by_id if key not in seen)
        return valid, failed

    def run(self, state, deadline, cache=None, save_checkpoint=None):
        cache = cache if cache is not None else {}
        save = save_checkpoint or (lambda: None)
        if cache and cache.get("audit", {}).get("pipeline") != PIPELINE:
            raise ProviderUnavailable("add_checkpoint_version_mismatch")
        audit = cache.setdefault("audit", {"pipeline": PIPELINE, "timings": {}, "items": [],
                                           "repair_used": False, "validation": []})
        errors = cache.setdefault("errors", [])
        if self.engine.judger is None:
            raise ProviderUnavailable("add_judger_not_configured")
        if "route" not in cache:
            started = monotonic()
            try:
                cache["route"] = self.engine.judger.route(state, deadline=deadline)
            except (OutputInvalid, ProviderUnavailable):
                cache["route"] = {}
                errors.append({"position": -1, "error": "route_unavailable"})
            audit["timings"]["route"] = monotonic() - started
            audit["route"] = cache["route"].get("route", {})
            save()
        route = cache["route"].get("route", {}).get("choice")
        if route not in {"single", "multiple"}:
            audit["disposition"] = route if route in {"raw_only", "uncertain"} else "route_unresolved"
            save()
            return [], errors, [audit]
        if "entries" not in cache:
            if route == "single":
                cache["entries"] = [{"item_id": "i0", "new_item": {"content": state["new_user_source"]["content"]}}]
                cache["categories"] = {"i0": cache["route"].get("category", {}), "i0_grounding": {"noul": 1.},
                    **{key: value for key, value in cache["route"].items() if key.startswith("i0_context_")}}
            else:
                started = monotonic()
                payload = {key: state[key] for key in ("new_user_source", "context_sources")}
                if "split_result" not in cache:
                    try:
                        cache["split_result"] = self.engine.model.split(payload, deadline=deadline)
                    except OutputInvalid:
                        cache["split_result"] = {}
                    save()
                entries, failures = self._split_entries(cache["split_result"])
                audit["validation"].extend({"stage": "split", **failure} for failure in failures)
                if failures and ("split_repair_result" in cache or not audit["repair_used"]):
                    if "split_repair_result" not in cache:
                        audit["repair_used"] = True
                        save()
                        try:
                            cache["split_repair_result"] = self.engine.model.split({**payload, "repair_errors": failures,
                                "accepted_items": [entry["new_item"] for entry in entries]}, deadline=deadline)
                        except (OutputInvalid, ProviderUnavailable):
                            cache["split_repair_result"] = {}
                        save()
                    repaired, remaining = self._split_entries(cache["split_repair_result"])
                    if cache["split_repair_result"]:
                        if not remaining:
                            entries, failures = repaired, []
                        else:
                            by_content = {entry["new_item"]["content"].strip(): entry for entry in entries}
                            for entry in repaired:
                                by_content.setdefault(entry["new_item"]["content"].strip(), entry)
                            entries = [{"item_id": f"i{index}", "new_item": entry["new_item"]}
                                       for index, entry in enumerate(list(by_content.values())[:24])]
                            failures = remaining
                    else:
                        failures.append({"position": -1, "error": "split_repair_unavailable"})
                    audit["validation"].extend({"stage": "split_repair", **failure} for failure in remaining)
                errors.extend(failures)
                cache["entries"] = entries
                audit["timings"]["split"] = monotonic() - started
            save()
        entries = cache["entries"]
        if not entries:
            audit["disposition"] = "split_unavailable"
            save()
            return [], errors, [audit]
        focused = [{**entry, "new_user_source": state["new_user_source"],
                    "context_sources": state["context_sources"]} for entry in entries]
        if "categories" not in cache:
            started = monotonic()
            classify_state = [{**entry} for entry in focused]
            classify_state[0]["split_contents"] = [entry["new_item"]["content"] for entry in entries]
            try:
                cache["categories"] = self.engine.judger.classify(classify_state, deadline=deadline)
            except (OutputInvalid, ProviderUnavailable):
                cache["categories"] = {}
                errors.append({"position": -1, "error": "classification_unavailable"})
            audit["timings"]["classify"] = monotonic() - started
            coverage = cache["categories"].get("split_coverage", {}).get("noul")
            audit["split_coverage"] = coverage
            if coverage is None or coverage <= .5:
                errors.append({"position": -1, "error": "split_coverage_unconfirmed"})
            save()
        if "recalled" not in cache:
            started = monotonic()
            selected = []
            audit["items"] = []
            for entry in focused:
                key = entry["item_id"]
                category = cache["categories"].get(key, {})
                kind = category.get("choice")
                grounding = cache["categories"].get(key + "_grounding", {}).get("noul")
                record = {"item_id": key, "source_id": state["new_user_source"]["id"],
                          "category": category, "grounding": grounding, "candidate_ids": []}
                audit["items"].append(record)
                if kind not in {"event", "fact", "preference", "experience"} or grounding is None or grounding <= .5:
                    record["disposition"] = kind if kind in {"raw_only", "uncertain"} else "classification_or_grounding_unresolved"
                    continue
                candidates = self.engine._typed_candidates(state, ContentItem.model_validate(entry["new_item"]), kind, deadline)
                contexts = [source for number, source in enumerate(entry["context_sources"])
                            if cache["categories"].get(f"{key}_context_{number}", {}).get("noul", 0) > .5]
                record["candidate_ids"] = [candidate["id"] for candidate in candidates]
                record["context_source_ids"] = [source["id"] for source in contexts]
                selected.append({**entry, "kind": kind, "candidates": candidates, "context_sources": contexts})
            cache["recalled"] = selected
            audit["timings"]["recall"] = monotonic() - started
            save()
        selected = cache["recalled"]
        if "choices" not in cache:
            started = monotonic()
            try:
                cache["choices"] = self.engine.judger.select(selected, deadline=deadline) if selected else {}
            except (OutputInvalid, ProviderUnavailable):
                cache["choices"] = {}
                errors.append({"position": -1, "error": "operation_choice_unavailable"})
            audit["timings"]["choose"] = monotonic() - started
            save()
        commands, edits = [], []
        records = {record["item_id"]: record for record in audit["items"]}
        for entry in selected:
            key = entry["item_id"]
            record = records[key]
            target_answer = cache["choices"].get(key + "_target", {})
            handle = target_answer.get("choice")
            target = next((candidate for candidate in entry["candidates"] if candidate["handle"] == handle), None)
            operation_answer = cache["choices"].get(key + "_" + handle, {}) if target else cache["choices"].get(key + "_new", {})
            operation = operation_answer.get("choice")
            record.update(target_choice=target_answer, operation_choice=operation_answer)
            if not ((target and operation in {"update", "retract"}) or (handle == "new" and operation == "create")):
                record["disposition"] = "keep" if operation == "keep" else "target_operation_unresolved"
                continue
            record.update(operation=operation, target_id=target["id"] if target else None)
            comparison = {**state, "candidates": entry["candidates"], "context_sources": entry["context_sources"],
                          "item_content": entry["new_item"]["content"]}
            if operation == "retract":
                fields = {"content": target["content"], "objects": [], "time_expression": None,
                          "end_expression": None, "status": "stated", "impact": "unknown"}
                commands.append((key, self.command(fields, entry, operation, target), comparison))
            else:
                edits.append({**entry, "operation": operation, "target": target, "comparison_state": comparison})
        if edits:
            started = monotonic()
            preceding = {}
            for edit in edits:
                identity = edit["target"]["id"] if edit["target"] else edit["item_id"]
                edit["preceding_assertions"] = list(preceding.get(identity, []))
                preceding.setdefault(identity, []).append(edit["new_item"])
            payload = {"edits": [{key: value for key, value in edit.items()
                                  if key not in {"comparison_state", "candidates"}} for edit in edits]}
            if "written" not in cache:
                try:
                    cache["written"] = self.engine.model.materialize(payload, deadline=deadline)
                except OutputInvalid:
                    cache["written"] = {}
                save()
            valid, failed = self._write_plan(cache["written"], edits)
            audit["validation"].extend({"stage": "write", "item_id": failure["item_id"], "error": failure["error"]}
                                       for failure in failed)
            if failed and ("write_repair_result" in cache or not audit["repair_used"]):
                repair_ids = {edit["item_id"] for edit in edits if edit["item_id"] not in valid}
                repair_edits = [edit for edit in edits if edit["item_id"] in repair_ids]
                repair_payload = {"edits": [edit for edit in payload["edits"] if edit["item_id"] in repair_ids],
                                  "repair_errors": [{key: failure[key] for key in ("item_id", "error")}
                                                    for failure in failed]}
                if repair_edits:
                    if "write_repair_result" not in cache:
                        audit["repair_used"] = True
                        save()
                        try:
                            cache["write_repair_result"] = self.engine.model.materialize(repair_payload, deadline=deadline)
                        except (OutputInvalid, ProviderUnavailable):
                            cache["write_repair_result"] = {}
                        save()
                    if cache["write_repair_result"]:
                        replacement, failed = self._write_plan(cache["write_repair_result"], repair_edits)
                        valid.update(replacement)
                        audit["validation"].extend({"stage": "write_repair", "item_id": key, "error": None}
                                                   for key in replacement)
                        audit["validation"].extend({"stage": "write_repair", "item_id": failure["item_id"],
                                                    "error": failure["error"]} for failure in failed)
            for failure in failed:
                if failure["error"] not in TIME_ERRORS:
                    continue
                edit = next((item for item in edits if item["item_id"] == failure["item_id"]), None)
                if edit is None:
                    continue
                raw = dict(failure.get("fields", {}))
                date_error = failure["error"]
                degraded = {}
                for _ in range(2):
                    field = "time_expression" if date_error == "time_unresolved_choose_supported_expression_or_null" else "end_expression"
                    raw[field] = None
                    degraded, remaining = self._write_plan({"items": [raw]}, [edit])
                    if degraded or not remaining or remaining[0]["error"] not in TIME_ERRORS:
                        break
                    date_error = remaining[0]["error"]
                if degraded:
                    valid.update(degraded)
                    audit["validation"].append({"stage": "time_degradation", "item_id": edit["item_id"],
                                                "error": "accepted_with_partial_or_unknown_time"})
            for edit in edits:
                key = edit["item_id"]
                if key in valid:
                    commands.append((key, valid[key], edit["comparison_state"]))
                else:
                    records[key]["disposition"] = "memory_write_failed"
            errors.extend({"position": -1, "error": failure["error"]} for failure in failed
                          if failure["item_id"] not in valid)
            audit["timings"]["materialize"] = monotonic() - started
        accepted = []
        for key, raw, comparison in sorted(commands, key=lambda entry: int(entry[0][1:])):
            try:
                accepted.append(self.engine._validate_command(raw, comparison))
                records[key]["disposition"] = "accepted"
            except (ValidationError, ValueError) as error:
                records[key]["disposition"] = "final_validation_failed"
                errors.append({"position": -1, "error": "command_schema_invalid"
                               if isinstance(error, ValidationError) else str(error)[:200]})
        save()
        return accepted, errors, [audit]
