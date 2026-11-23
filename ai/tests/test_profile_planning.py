"""Synthetic bounded invariants for the local planner, using the existing runner."""

from dataclasses import replace
from itertools import product
import json
from pathlib import Path
import sys
from typing import Final
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from profile_planning import (
    AnalyzeRequest, Binding, Corpus, Limits, PersonaId, Plan, PlanningError,
    RecordId, SnapshotId, Source, TextVersionId, plan_leaves, validate_coverage,
)

BINDING: Final = Binding(PersonaId(UUID(int=1)), 7, SnapshotId(UUID(int=2)),
                         "model-a", "tokenizer-b", "template-c")
LIMITS: Final = Limits(1000, 100, 10000, 100, 20, 100, 16000, 100000, 100000, 100000, 7)


def corpus(*texts: str) -> Corpus:
    return Corpus(BINDING, (Source(RecordId(UUID(int=3)), TextVersionId(UUID(int=4)), texts),))


def count_full_prompt(request: AnalyzeRequest) -> int:
    """Synthetic tokenizer: one token per serialized code point plus 11 fixed tokens."""
    return len(request.body().decode("utf-8")) + 11


class ProfilePlanningTests(unittest.TestCase):
    def test_unicode_ranges_reconstruct_each_original_exactly(self) -> None:
        original = Corpus(BINDING, (*corpus(" \uac00\U0001f600e\u0301\tZ \n", '"\\\nB').sources,
                                  Source(RecordId(UUID(int=5)), TextVersionId(UUID(int=6)),
                                         ("\u03a9\u2003D ",))))
        limits = replace(LIMITS, characters_per_utterance=4, utterances_per_source=1,
                         sources_per_request=1)
        plan = plan_leaves(original, limits, count_full_prompt)
        rebuilt: dict[tuple[int, int], str] = {}
        coordinates: list[tuple[int, int, int]] = []
        for leaf in plan.leaves:
            texts = [t for s in leaf.request.sources for t in s.target_utterances]
            self.assertLessEqual(len(leaf.request.sources), 1)
            self.assertLessEqual(len(texts), 1)
            for r, text in zip(leaf.ranges, texts, strict=True):
                self.assertTrue(text.strip())
                self.assertLessEqual(len(text), 4)
                key = (r.source_index, r.utterance_index)
                rebuilt[key] = rebuilt.get(key, "") + text
                coordinates.extend((*key, c) for c in range(r.start, r.end))
        expected = {(si, ui): text for si, s in enumerate(original.sources)
                    for ui, text in enumerate(s.target_utterances)}
        self.assertEqual(rebuilt, expected)
        self.assertEqual(coordinates, [(si, ui, c) for (si, ui), text in expected.items()
                                       for c in range(len(text))])
        validate_coverage(original, plan)

    def test_full_request_overhead_and_both_context_limits_include_output(self) -> None:
        original = corpus("x")
        sample = plan_leaves(original, LIMITS, count_full_prompt).leaves[0].request
        exact = count_full_prompt(sample) + LIMITS.reserved_output_tokens
        for field in ("model_context_tokens", "server_context_tokens"):
            with self.subTest(field=field):
                limits = replace(LIMITS, **{field: exact})
                self.assertEqual(len(plan_leaves(original, limits, count_full_prompt).leaves), 1)
                with self.assertRaises(PlanningError) as caught:
                    plan_leaves(original, replace(limits, **{field: exact - 1}), count_full_prompt)
                self.assertEqual(caught.exception.code, "infeasible")

    def test_wire_body_uses_utf8_bytes_and_only_proposed_fields(self) -> None:
        original = corpus("\U0001f600\n\"\\")
        request = plan_leaves(original, LIMITS, count_full_prompt).leaves[0].request
        expected = {
            "request_id": str(request.request_id), "node_id": str(request.node_id),
            "persona_id": str(BINDING.persona_id), "source_revision": 7,
            "source_snapshot_id": str(BINDING.source_snapshot_id),
            "sources": [{"record_id": str(UUID(int=3)), "text_version_id": str(UUID(int=4)),
                         "target_utterances": ["\U0001f600\n\"\\"]}],
        }
        self.assertEqual(json.loads(request.body()), expected)
        size = len(request.body())
        self.assertGreater(size, len(request.body().decode("utf-8")))
        limits = replace(LIMITS, body_bytes=size, leaves=1)
        self.assertEqual(len(plan_leaves(original, limits, count_full_prompt).leaves), 1)
        with self.assertRaises(PlanningError) as caught:
            plan_leaves(original, replace(limits, body_bytes=size - 1), count_full_prompt)
        self.assertEqual(caught.exception.code, "infeasible")

    def test_source_and_utterance_counts_force_ordered_partition(self) -> None:
        original = Corpus(BINDING, (corpus("a", "b", "c").sources[0],
                                   Source(RecordId(UUID(int=5)), TextVersionId(UUID(int=6)),
                                          ("d", "e"))))
        for source_cap, utterance_cap, expected_leaves in ((1, 1, 5), (1, 2, 3), (2, 3, 1)):
            with self.subTest(caps=(source_cap, utterance_cap)):
                limits = replace(LIMITS, sources_per_request=source_cap,
                                 utterances_per_source=utterance_cap)
                plan = plan_leaves(original, limits, count_full_prompt)
                self.assertEqual(len(plan.leaves), expected_leaves)
                validate_coverage(original, plan)

    def test_nonmonotone_counts_require_backtracking_past_longest_prefix(self) -> None:
        original = corpus("abcd")

        def count(request: AnalyzeRequest) -> int:
            text = request.sources[0].target_utterances[0]
            return 1 if len(text) in (2, 3) else 100

        plan = plan_leaves(original, replace(LIMITS, model_context_tokens=8), count)
        self.assertEqual([s.target_utterances for leaf in plan.leaves
                          for s in leaf.request.sources], [("ab",), ("cd",)])
        validate_coverage(original, plan)

    def test_finite_feasibility_matches_independent_partition_oracle(self) -> None:
        # All 64 arbitrary accept/reject assignments for the six intervals of "abc".
        text = "abc"
        intervals = ("a", "b", "c", "ab", "bc", "abc")
        partitions = (("abc",), ("a", "bc"), ("ab", "c"), ("a", "b", "c"))
        for mask in product((False, True), repeat=6):
            allowed = {part for part, fits in zip(intervals, mask, strict=True) if fits}

            def count(request: AnalyzeRequest) -> int:
                return 1 if request.sources[0].target_utterances[0] in allowed else 100

            for cap in (1, 2, 3):
                with self.subTest(allowed=allowed, cap=cap):
                    lengths = [len(p) for p in partitions if len(p) <= cap
                               and all(piece in allowed for piece in p)]
                    limits = replace(LIMITS, leaves=cap, model_context_tokens=8)
                    if lengths:
                        plan = plan_leaves(corpus(text), limits, count)
                        self.assertEqual(len(plan.leaves), min(lengths))
                        validate_coverage(corpus(text), plan)
                    else:
                        with self.assertRaises(PlanningError) as caught:
                            plan_leaves(corpus(text), limits, count)
                        self.assertEqual(caught.exception.code, "infeasible")

    def test_exhaustive_unicode_roundtrip_preserves_whitespace(self) -> None:
        for size in range(1, 5):
            for chars in product(("x", "\uac00", "\U0001f600", " "), repeat=size):
                text = "".join(chars)
                if not text.strip():
                    with self.assertRaises(PlanningError):
                        corpus(text)
                    continue
                for cap in (1, 2, 3, 4):
                    feasible_lengths = []
                    for cuts in product((False, True), repeat=size - 1):
                        edges = [0, *(i + 1 for i, cut in enumerate(cuts) if cut), size]
                        pieces = [text[a:b] for a, b in zip(edges, edges[1:])]
                        if all(piece.strip() and len(piece) <= cap for piece in pieces):
                            feasible_lengths.append(len(pieces))
                    with self.subTest(text=text, cap=cap):
                        original = corpus(text)
                        limits = replace(LIMITS, characters_per_utterance=cap)
                        if not feasible_lengths:
                            with self.assertRaises(PlanningError) as caught:
                                plan_leaves(original, limits, count_full_prompt)
                            self.assertEqual(caught.exception.code, "infeasible")
                            continue
                        plan = plan_leaves(original, limits, count_full_prompt)
                        self.assertEqual(len(plan.leaves), min(feasible_lengths))
                        self.assertEqual("".join(t for leaf in plan.leaves
                                                 for s in leaf.request.sources
                                                 for t in s.target_utterances), text)
                        validate_coverage(original, plan)

    def test_whitespace_cannot_be_dropped_to_make_a_fragment_fit(self) -> None:
        with self.assertRaises(PlanningError) as caught:
            plan_leaves(corpus(" a "), replace(LIMITS, characters_per_utterance=2),
                        count_full_prompt)
        self.assertEqual(caught.exception.code, "infeasible")

    def test_invalid_corpus_and_binding_fail_safely(self) -> None:
        source = corpus("private").sources[0]
        malformed = ((), (replace(source, target_utterances=()),),
                     (replace(source, target_utterances=("",)),),
                     (replace(source, target_utterances=(" \t\n",)),),
                     (replace(source, target_utterances=("\ud800",)),),
                     (source, source),
                     (source, replace(source, record_id=RecordId(UUID(int=5)))),
                     (source, replace(source, text_version_id=TextVersionId(UUID(int=6)))))
        for sources in malformed:
            with self.subTest(sources=sources), self.assertRaises(PlanningError) as caught:
                Corpus(BINDING, sources)
            self.assertEqual(caught.exception.code, "input")
            self.assertNotIn("private", str(caught.exception))
        for binding_field, value in (("source_revision", -1), ("source_revision", True),
                                     ("model_revision", ""),
                                     ("tokenizer_revision", " "), ("template_revision", "")):
            with self.subTest(field=binding_field), self.assertRaises(PlanningError):
                replace(BINDING, **{binding_field: value})

    def test_work_bounds_and_invalid_counter_fail_explicitly(self) -> None:
        for limits, code in ((replace(LIMITS, input_characters=1), "limits"),
                             (replace(LIMITS, input_utterances=1), "limits"),
                             (replace(LIMITS, candidates=1, utterances_per_source=1),
                              "search_budget")):
            with self.subTest(code=code), self.assertRaises(PlanningError) as caught:
                plan_leaves(corpus("a", "b"), limits, count_full_prompt)
            self.assertEqual(caught.exception.code, code)
        for limits_field, value in (("body_bytes", 0), ("sources_per_request", 21),
                                   ("utterances_per_source", 101), ("characters_per_utterance", 16001),
                                   ("reserved_output_tokens", 0), ("candidates", 0)):
            with self.subTest(field=limits_field), self.assertRaises(PlanningError):
                replace(LIMITS, **{limits_field: value})
        for value in (-1, True):
            with self.subTest(count=value), self.assertRaises(PlanningError) as caught:
                plan_leaves(corpus("a"), LIMITS, lambda request: value)
            self.assertEqual(caught.exception.code, "counter")

    def test_independent_validator_rejects_malformed_manifests(self) -> None:
        original = corpus("ab", "cd")
        valid = plan_leaves(original, replace(LIMITS, characters_per_utterance=1,
                                             utterances_per_source=1), count_full_prompt)
        first, second, *rest = valid.leaves
        r = first.ranges[0]
        s = first.request.sources[0]
        bad_first = (
            replace(first, ranges=(replace(r, start=1),)),  # gap
            replace(first, ranges=(replace(r, end=3),)),  # outside original
            replace(first, ranges=(replace(r, source_index=1),)),
            replace(first, ranges=(replace(r, utterance_index=1),)),
            replace(first, ranges=()),
            replace(first, request=replace(first.request, sources=(replace(s, target_utterances=("z",)),))),
            replace(first, request=replace(first.request, sources=(replace(s, record_id=RecordId(UUID(int=9))),))),
            replace(first, request=replace(first.request, sources=(replace(s, text_version_id=TextVersionId(UUID(int=9))),))),
            replace(first, request=replace(first.request, sources=(s, s))),
        )
        bad_leaves = [(b, second, *rest) for b in bad_first]
        bad_leaves.extend(((), (first, first, second, *rest), (second, first, *rest),
                           (first, *rest), valid.leaves[:-1],
                           (first, replace(second, request=replace(second.request, node_id=first.request.node_id)), *rest),
                           (first, replace(second, request=replace(second.request, request_id=first.request.request_id)), *rest),
                           (first, replace(second, ranges=(replace(second.ranges[0], start=0),)), *rest)))
        for leaves in bad_leaves:
            with self.subTest(leaves=leaves), self.assertRaises(PlanningError) as caught:
                validate_coverage(original, Plan(BINDING, leaves))
            self.assertEqual(caught.exception.code, "coverage")

    def test_binding_changes_invalidate_plan_and_leaf(self) -> None:
        original = corpus("a")
        plan = plan_leaves(original, LIMITS, count_full_prompt)
        changes = (replace(BINDING, source_revision=8),
                   replace(BINDING, source_snapshot_id=SnapshotId(UUID(int=8))),
                   replace(BINDING, persona_id=PersonaId(UUID(int=8))),
                   replace(BINDING, model_revision="different"),
                   replace(BINDING, tokenizer_revision="different"),
                   replace(BINDING, template_revision="different"))
        for binding in changes:
            leaf = plan.leaves[0]
            for changed in (replace(plan, binding=binding),
                            replace(plan, leaves=(replace(leaf, request=replace(
                                leaf.request, binding=binding)),))):
                with self.subTest(binding=binding), self.assertRaises(PlanningError):
                    validate_coverage(original, changed)
