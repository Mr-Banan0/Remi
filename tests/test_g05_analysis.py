from scripts.analyze_golden import analyze


def test_analysis_reads_staged_validation_without_guessing_a_memory_kind():
    report = {"complete": True, "source_to_ref": {}, "questions": [], "lifecycle_searches": [], "diagnostic_calls": [],
              "derivation": {"rhelm": {"memories": [], "requests": [{"units": {"0": {
                  "commands": [], "rejected": [], "validation": [{"pipeline": "G05-jev-choice", "validation": [
                      {"stage": "prepare", "error": "context_source_unknown"}]}]}}}]}}}
    result = analyze(report)
    assert result["validation_failures_by_stage"] == {"prepare|context_source_unknown": 1}
    assert result["validation_failures_by_kind"] == {"unclassified_prepare|context_source_unknown": 1}
