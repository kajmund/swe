"""Offline scorer for the frozen Research Gym policy comparison.

This package does not call a model, a network, or SWE core.
"""

from hy4_research_policy.cost import compare_with_deepseek
from hy4_research_policy.estimate import estimate_matrix
from hy4_research_policy.live import LiveDispatchRefused, refuse_live_dispatch
from hy4_research_policy.score import score_trace

__all__ = [
    "LiveDispatchRefused",
    "compare_with_deepseek",
    "estimate_matrix",
    "refuse_live_dispatch",
    "score_trace",
]
