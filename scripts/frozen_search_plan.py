"""Experiment-only replay of recorded planner decisions, preserving repair order."""
from copy import deepcopy

from remi.providers import ProviderUnavailable


class FrozenSearchPlanner:
    def __init__(self):
        self.calls = []
        self.outputs = []

    def load(self, trace):
        attempts = trace.get('planner_attempts', [])
        if not attempts or any('raw' not in attempt for attempt in attempts):
            raise ValueError('complete_recorded_planner_attempts_required')
        self.outputs = [deepcopy(attempt['raw']) for attempt in attempts]

    def decide(self, state, *, deadline):
        if not self.outputs:
            raise ProviderUnavailable('recorded_planner_attempts_exhausted')
        return self.outputs.pop(0)
