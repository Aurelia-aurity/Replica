"""Synthetic callback cost and complete fallback for natural-boundary planning."""
from dataclasses import replace
import unittest

from test_profile_planning import LIMITS, corpus, count_full_prompt
from profile_planning import AnalyzeRequest, PlanningError, plan_leaves, validate_coverage


class ProfilePlanningCostTests(unittest.TestCase):
    def test_many_utterances_bound_complete_prompt_callback_cost(self) -> None:
        original = corpus(*(f"turn {i:02d}: a short line." for i in range(20)))
        calls = 0

        def count(request: AnalyzeRequest) -> int:
            nonlocal calls
            calls += 1
            return count_full_prompt(request)

        limits = replace(LIMITS, model_context_tokens=600)
        plan = plan_leaves(original, limits, count)
        validate_coverage(original, plan)
        self.assertEqual(len(plan.leaves), 3)
        self.assertLessEqual(calls, 100)

    def test_natural_search_falls_back_to_nonmonotone_intra_utterance_cuts(self) -> None:
        original = corpus("ab", "cd")

        def count(request: AnalyzeRequest) -> int:
            pieces = request.sources[0].target_utterances
            return 1 if pieces in (("a",), ("b", "c"), ("d",)) else 100

        limits = replace(LIMITS, leaves=3, model_context_tokens=8)
        plan = plan_leaves(original, limits, count)
        validate_coverage(original, plan)
        self.assertEqual([s.target_utterances for leaf in plan.leaves
                          for s in leaf.request.sources], [("a",), ("b", "c"), ("d",)])
        validate_coverage(original, plan_leaves(original, replace(limits, candidates=10), count))
        for bound, code in ((replace(limits, leaves=2), "infeasible"),
                            (replace(limits, candidates=2), "search_budget"),
                            (replace(limits, candidates=9), "search_budget")):
            with self.subTest(code=code), self.assertRaises(PlanningError) as caught:
                plan_leaves(original, bound, count)
            self.assertEqual(caught.exception.code, code)

    def test_natural_cuts_take_priority_over_fewer_intra_utterance_leaves(self) -> None:
        original = corpus("ab", "cd", "ef")

        def count(request: AnalyzeRequest) -> int:
            pieces = request.sources[0].target_utterances
            return 1 if pieces in (("ab",), ("cd",), ("ef",),
                                    ("ab", "c"), ("d", "ef")) else 100

        limits = replace(LIMITS, leaves=3, model_context_tokens=8)
        natural = plan_leaves(original, limits, count)
        validate_coverage(original, natural)
        self.assertEqual(len(natural.leaves), 3)
        shorter = plan_leaves(original, replace(limits, leaves=2), count)
        validate_coverage(original, shorter)
        self.assertEqual(len(shorter.leaves), 2)
