"""Mechanical checks of learning examples; these are not model-quality evals."""
from __future__ import annotations

from collections import Counter

import pytest

from .helpers import TestEngine as MemoryEngine
from embergraph.models import Command
from .command_contract_examples import ADD_EXAMPLES
from embergraph.add_judgments import SPLIT_PROMPT, WRITE_PROMPT


@pytest.mark.parametrize("example", ADD_EXAMPLES, ids=lambda item: item["example"])
def test_synthetic_examples_follow_the_live_command_and_source_contract(example):
    commands = [Command.model_validate(raw) for raw in example["output"]["commands"]]
    state = example["input"]
    assert state["new_user_source"]["id"].startswith("synthetic_source_")
    assert all(item["id"].startswith("synthetic_memory_") for item in state["candidates"])
    assert all(item["id"].startswith("synthetic_context_") for item in state["context_sources"])
    engine = MemoryEngine()
    for command in commands:
        checked = engine._validate_command(command.model_dump(), state)
        assert checked == command
    engine.close()


def test_learning_examples_cover_operations_and_memory_kinds_with_bounded_prompt_size():
    commands = [command for example in ADD_EXAMPLES for command in example["output"]["commands"]]
    assert Counter(command["operation"] for command in commands) == {"create": 5, "update": 4, "retract": 1}
    assert {command["kind"] for command in commands} == {"preference", "fact", "event", "experience"}
    assert any(not example["output"]["commands"] for example in ADD_EXAMPLES)
    assert len(SPLIT_PROMPT.encode("utf-8")) < 8000
    assert len(WRITE_PROMPT.encode("utf-8")) < 8000


def test_changed_claim_reaffirmation_and_distinct_occurrence_use_separate_examples():
    by_name = {example["example"]: example for example in ADD_EXAMPLES}
    confirmation = by_name["same_preference_reaffirmation"]
    assert confirmation["output"]["commands"][0]["operation"] == "update"
    assert confirmation["output"]["commands"][0]["content"] == confirmation["input"]["candidates"][0]["content"]
    new_occurrence = by_name["distinct_occurrence_create"]["output"]["commands"][0]
    assert new_occurrence["operation"] == "create" and new_occurrence["target_id"] is None
