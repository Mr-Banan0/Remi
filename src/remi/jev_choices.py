"""Typed Jev judgments for Add, using the same verified native gateway as reranking."""
from __future__ import annotations

import math
import json
from collections import deque
from time import monotonic

import httpx

from .add_judgments import (CATEGORY_CRITERIA, GROUNDING, NEW_CRITERIA, OPERATION,
                            OPERATION_CRITERIA, TARGET, ROUTE, ROUTE_CRITERIA, SPLIT_COVERAGE, CONTEXT_SUPPORT,
                            category_instructions)
from .providers import OutputInvalid, ProviderUnavailable, error_label, remaining


def choice_answer(answer, criteria):
    if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in criteria:
        raise OutputInvalid("choice_answer_invalid")
    probabilities, confidence = answer.get("probabilities"), answer.get("confidence")
    if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
        raise OutputInvalid("choice_probability_options_invalid")
    values = [*probabilities.values(), confidence]
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise OutputInvalid("choice_probability_invalid")
    if abs(sum(probabilities.values()) - 1) > .02:
        raise OutputInvalid("choice_probability_sum_invalid")
    if probabilities[answer["choice"]] + .02 < max(probabilities.values()):
        raise OutputInvalid("choice_not_top_option")
    return {"choice": answer["choice"], "probabilities": probabilities, "confidence": confidence}


class JevChoices:
    def __init__(self, api_key, base_url, model="jev-1.13.0", timeout=8, transport=None):
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("jev_base_url_invalid")
        self.model, self.timeout = model, timeout
        self.client = httpx.Client(base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + api_key}, timeout=timeout, transport=transport)
        self.calls = deque(maxlen=2048)

    def _ask(self, state, questions, kind, deadline):
        started = monotonic()
        trace = {"kind": kind, "model": self.model, "question_count": len(questions)}
        try:
            response = self.client.post("systemone", json={"model": self.model, "state": state, "questions": questions},
                                        timeout=remaining(deadline, self.timeout))
            trace["http_status"] = response.status_code
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
                raise OutputInvalid("jev_answers_invalid")
            result, invalid = {}, 0
            for key, question in questions.items():
                answer = body["answers"].get(key)
                try:
                    if question["type"] == "choice":
                        result[key] = choice_answer(answer, question["criteria"])
                    elif (isinstance(answer, dict) and answer.get("type") == "noul"
                          and type(answer.get("noul")) in (int, float)
                          and math.isfinite(answer["noul"]) and 0 <= answer["noul"] <= 1):
                        result[key] = {"noul": answer["noul"]}
                    else:
                        raise OutputInvalid("noul_answer_invalid")
                except OutputInvalid:
                    invalid += 1
            trace["invalid_answers"] = invalid
            trace["input_tokens"] = body.get("usage", {}).get("input_tokens")
            return result
        except (OutputInvalid, ProviderUnavailable) as error:
            trace.update(error_label(error))
            raise
        except Exception as error:
            trace.update(error_label(error))
            raise ProviderUnavailable("jev_choice_unavailable") from error
        finally:
            trace["seconds"] = monotonic() - started
            self.calls.append(trace)

    @staticmethod
    def groups(states):
        batch = []
        for state in states:
            if len(json.dumps({"items": [state]}, ensure_ascii=False).encode()) > 24000:
                continue
            if batch and (len(batch) == 4 or len(json.dumps({"items": [*batch, state]}, ensure_ascii=False).encode()) > 24000):
                yield batch
                batch = []
            batch.append(state)
        if batch:
            yield batch

    def classify(self, states, *, deadline):
        result = {}
        self._record_oversized(states, "add_classify")
        for group in self.groups(states):
            try:
                result.update(self._classify(group, deadline=deadline))
            except (OutputInvalid, ProviderUnavailable):
                continue
        return result

    def route(self, state, *, deadline):
        """The speculative category is consumed only when the single-target branch is selected."""
        focused = {key: state[key] for key in ("new_user_source", "context_sources")}
        if len(json.dumps(focused, ensure_ascii=False).encode()) > 24000:
            self.calls.append({"kind": "add_route", "model": self.model, "question_count": 0,
                               "seconds": 0, "skipped": True, "error_type": "state_budget_exceeded"})
            return {}
        questions = {
            "route": {"type": "choice", "instructions": ROUTE, "criteria": ROUTE_CRITERIA},
            "category": {"type": "choice", "instructions": category_instructions(
                "new_user_source.content", "new_user_source", "context_sources", single_target=True),
                "criteria": CATEGORY_CRITERIA},
        }
        for index, source in enumerate(focused["context_sources"]):
            questions[f"i0_context_{index}"] = {"type": "noul", "instructions": (
                f"If new_user_source establishes one target, focus on it and context_sources[{index}]. " + CONTEXT_SUPPORT)}
        return self._ask(focused, questions, "add_route", deadline)

    def _classify(self, states, *, deadline):
        questions = {}
        for index, state in enumerate(states):
            focus = f"items[{index}]"
            key = state["item_id"]
            questions[key] = {"type": "choice", "instructions": category_instructions(
                f"{focus}.new_item.content", f"{focus}.new_user_source", f"{focus}.context_sources"),
                              "criteria": CATEGORY_CRITERIA}
            questions[key + "_grounding"] = {"type": "noul", "instructions": f"Focus only {focus}. " + GROUNDING}
            for number, source in enumerate(state.get("context_sources", [])):
                questions[f"{key}_context_{number}"] = {"type": "noul", "instructions": (
                    f"Focus only {focus}.new_item with its new_user_source and {focus}.context_sources[{number}]. " + CONTEXT_SUPPORT)}
        if states and "split_contents" in states[0]:
            questions["split_coverage"] = {"type": "noul", "instructions": SPLIT_COVERAGE}
        return self._ask({"items": states}, questions, "add_classify", deadline)

    def select(self, states, *, deadline):
        result = {}
        self._record_oversized(states, "add_choice")
        for group in self.groups(states):
            try:
                result.update(self._select(group, deadline=deadline))
            except (OutputInvalid, ProviderUnavailable):
                continue
        return result

    def _record_oversized(self, states, kind):
        for state in states:
            if len(json.dumps({"items": [state]}, ensure_ascii=False).encode()) > 24000:
                self.calls.append({"kind": kind, "model": self.model, "question_count": 0,
                                   "seconds": 0, "skipped": True, "error_type": "state_budget_exceeded"})

    def _select(self, states, *, deadline):
        questions = {}
        for index, state in enumerate(states):
            focus, key = f"items[{index}]", state["item_id"]
            criteria = {candidate["handle"]: f"{focus}.candidates[{number}] is the same claim or occurrence."
                        for number, candidate in enumerate(state["candidates"])}
            criteria.update(new="No candidate represents this particular claim or occurrence.",
                            uncertain="Target identity is not established or several targets remain indistinguishable.")
            questions[key + "_target"] = {"type": "choice", "instructions": f"Focus only {focus}. " + TARGET,
                                         "criteria": criteria}
            questions[key + "_new"] = {"type": "choice", "instructions": f"Focus only {focus}. If none of its candidates is the same item, what is supported by the new user assertion?",
                                      "criteria": NEW_CRITERIA}
            for number, candidate in enumerate(state["candidates"]):
                questions[key + "_" + candidate["handle"]] = {"type": "choice",
                    "instructions": f"Focus only {focus}.new_item and {focus}.candidates[{number}]. " + OPERATION,
                    "criteria": OPERATION_CRITERIA}
        return self._ask({"items": states}, questions, "add_choice", deadline)

    def close(self):
        self.client.close()
