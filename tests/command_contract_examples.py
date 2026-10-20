"""Learning-only synthetic examples, authored independently of evaluation records."""
from __future__ import annotations


def _example(name, text, kind=None, operation="create", content=None, old=None, context=None, time_expression=None):
    source_id = f"synthetic_source_{name}"
    target_id = f"synthetic_memory_{name}" if old else None
    inputs = {
        "new_user_source": {"id": source_id, "role": "user", "content": text,
                            "message_at": "2027-02-09T10:00:00+00:00"},
        "context_sources": [] if context is None else [
            {"id": f"synthetic_context_{name}", "role": "assistant", "content": context,
             "message_at": "2027-02-09T09:59:00+00:00"}],
        "candidates": [] if old is None else [
            {"id": target_id, "kind": old[0], "content": old[1],
             "status": "occurred" if old[0] == "event" else "stated"}],
    }
    commands = [] if kind is None else [{
        "operation": operation, "target_id": target_id if operation != "create" else None,
        "kind": kind, "content": content, "source_id": source_id,
        "context_source_ids": [] if context is None else [f"synthetic_context_{name}"],
        "quote": text, "objects": [{"kind": "user", "name": "user"}],
        "time_expression": time_expression, "end_expression": None,
        "status": "occurred" if kind == "event" else "stated", "impact": "unknown",
    }]
    return {"example": name, "input": inputs, "output": {"commands": commands}}


# These are prompt teaching material. Model-quality evaluation uses separate records.
ADD_EXAMPLES = (
    _example(
        "unrelated_claim_create",
        "I prefer numbered steps in maintenance notes.",
        kind="preference", content="user prefers numbered steps in maintenance notes.",
        old=("fact", "user holds a category R4 soldering qualification."),
    ),
    _example(
        "same_preference_update",
        "Starting today, use an annotated diagram as my default for maintenance notes.",
        kind="preference", operation="update",
        content="user prefers an annotated diagram as the default format for maintenance notes.",
        old=("preference", "user prefers numbered steps in maintenance notes."),
        time_expression="today",
    ),
    _example(
        "same_preference_reaffirmation",
        "I still prefer an annotated diagram for maintenance notes.",
        kind="preference", operation="update",
        content="user prefers an annotated diagram as the default format for maintenance notes.",
        old=("preference", "user prefers an annotated diagram as the default format for maintenance notes."),
    ),
    _example(
        "same_occurrence_details",
        "That R-28 filter test passed; the measured drop was 18 pascals.",
        kind="event", operation="update",
        content="user performed the R-28 filter test, which passed with a measured drop of 18 pascals.",
        old=("event", "user performed the R-28 filter test; its measured result is awaiting recording."),
    ),
    _example(
        "distinct_occurrence_create",
        "I ran a separate R-28 filter test with a replacement cartridge.",
        kind="event",
        content="user ran a separate R-28 filter test with a replacement cartridge.",
        old=("event", "user performed the completed R-28 filter test with the original cartridge."),
    ),
    _example(
        "ongoing_condition_create",
        "My editing account is locked until a supervisor verifies my identity.",
        kind="fact",
        content="user's editing account is locked until a supervisor verifies their identity.",
    ),
    _example(
        "same_condition_update",
        "My editing account was unlocked today; identity verification is complete.",
        kind="fact", operation="update",
        content="user's editing account is unlocked following completed identity verification.",
        old=("fact", "user's editing account is locked until a supervisor verifies their identity."),
        time_expression="today",
    ),
    _example(
        "false_claim_retract",
        "Correction: I was never certified to operate kiln K-6. Withdraw that claim.",
        kind="fact", operation="retract",
        content="user withdraws the claim that they are certified to operate kiln K-6.",
        old=("fact", "user is certified to operate kiln K-6."),
    ),
    _example(
        "explicit_context_confirmation",
        "I choose the third layout as my default.",
        kind="preference", content="user chooses a two-column comparison as their default layout.",
        context="The layouts are single-column prose, cards, and a two-column comparison, in that order.",
    ),
    _example(
        "format_request_raw_only",
        "Align the columns in your last answer.",
        old=("fact", "user holds a category R4 soldering qualification."),
    ),
    _example(
        "explicit_lesson_create",
        "Checksum validation caught a corrupted archive before upload. I'll validate checksums before future uploads.",
        kind="experience",
        content="user found that checksum validation caught a corrupted archive before upload and will validate checksums before future uploads.",
    ),
)
