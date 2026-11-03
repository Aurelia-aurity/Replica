"""#59 DEV source fixtures, fixed vectors and deterministic context budgets."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import unittest
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag.retrieval_v2 import (
    THRESHOLD, ContextConfig, contextual_evidence, evidence_json, exact_search, restore_context,
)
from rag.types import Chunk, Hit, Record, Retrieval

FIXTURE = Path(__file__).parent / "fixtures/retrieval_v2/source.json"


def fixture():
    """#59's UUID5 rule is used only to reconstruct its frozen test fixture."""
    data = json.loads(FIXTURE.read_text())
    records, chunks = [], []
    for row in data["records"]:
        records.append(Record(row["record_id"], row["text_version_id"], data["persona_id"],
                              row["kind"], len(row["turns"])))
        groups = [(row["speaker"], text, ())
                  for text in row["text"].split("\n\n") if text]
        groups.extend((t["speaker"], t["text"], (i,)) for i, t in enumerate(row["turns"]))
        for ordinal, (speaker, text, turns) in enumerate(groups, 1):
            role = "provider" if speaker == "나" else (
                "persona" if speaker == data["persona_speaker"] else "other")
            chunks.append(Chunk(
                str(uuid5(NAMESPACE_URL, f'{row["text_version_id"]}:{ordinal}')),
                row["record_id"], row["text_version_id"], text, role,
                data["persona_id"], speaker, row["date"], ordinal, turns))
    return tuple(records), tuple(chunks)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.records, self.chunks = fixture()

    def test_threshold_below_equal_above_preserves_low_scoring_record(self):
        for score, empty in ((THRESHOLD - 1e-10, True), (THRESHOLD, False),
                             (THRESHOLD + 1e-10, False)):
            with self.subTest(score=score):
                trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, score),
                                         Hit(self.chunks[2].chunk_id, 0.1)))
                result = restore_context(trace, self.chunks, self.records, lambda _: 10)
                self.assertEqual(result.predicted_no_evidence, empty)
                expected = () if empty else tuple(contextual_evidence(c) for c in self.chunks[:6])
                self.assertEqual(result.evidence, expected)

    def test_overlapping_chat_windows_restore_original_ids_roles_and_order(self):
        trace = Retrieval("p1", (Hit(self.chunks[3].chunk_id, 0.9),
                                 Hit(self.chunks[4].chunk_id, 0.8)))
        result = restore_context(trace, tuple(reversed(self.chunks)), self.records,
                                 lambda _: 10, ContextConfig(k=2, window=1))
        expected = tuple(contextual_evidence(c) for c in self.chunks[2:6])
        self.assertEqual(result.evidence, expected)
        self.assertEqual(len({e.chunk_id for e in result.evidence}), 4)

    def test_pretty_json_payload_preserves_five_field_token_input(self):
        item = contextual_evidence(self.chunks[0])
        rendered = evidence_json((item,))
        self.assertEqual(json.loads(rendered), [asdict(item)])
        self.assertEqual(rendered, json.dumps([asdict(item)], ensure_ascii=False, indent=2))

    def test_diary_expands_whole_then_falls_back_to_hits_when_oversized(self):
        trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, 0.9),))
        counter = lambda text: 100 * len(json.loads(text))
        result = restore_context(trace, self.chunks, self.records, counter,
                                 ContextConfig(budget=100))
        self.assertEqual(result.topk_evidence,
                         tuple(contextual_evidence(c) for c in self.chunks[:2]))
        self.assertEqual(result.evidence, (contextual_evidence(self.chunks[0]),))

    def test_budget_drops_lowest_record_not_oldest(self):
        trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, 0.9),
                                 Hit(self.chunks[4].chunk_id, 0.8)))
        counter = lambda text: 100 * len(json.loads(text))
        result = restore_context(trace, self.chunks, self.records, counter,
                                 ContextConfig(budget=600))
        self.assertEqual(result.evidence,
                         tuple(contextual_evidence(c) for c in self.chunks[:2]))

    def test_record_score_tie_drops_lowest_uuid(self):
        trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, 0.9),
                                 Hit(self.chunks[4].chunk_id, 0.9)))
        counter = lambda text: 100 * len(json.loads(text))
        result = restore_context(trace, self.chunks, self.records, counter,
                                 ContextConfig(budget=600))
        self.assertEqual(result.evidence,
                         tuple(contextual_evidence(c) for c in self.chunks[2:]))

    def test_unretainable_record_and_unresolvable_hit_fail_not_abstain(self):
        trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, 0.9),))
        with self.assertRaises(ValueError):
            restore_context(trace, self.chunks, self.records, lambda _: 2001)
        with self.assertRaises(KeyError):
            restore_context(trace, (), self.records, len)

    def test_empty_and_foreign_archive_and_wrong_versions(self):
        empty = restore_context(Retrieval("p2", ()), self.chunks, self.records, len)
        self.assertTrue(empty.predicted_no_evidence)
        trace = Retrieval("p1", (Hit(self.chunks[0].chunk_id, 0.9),))
        foreign = replace(self.chunks[1], persona_id="p2")
        result = restore_context(trace, (self.chunks[0], foreign), self.records, lambda _: 1)
        self.assertEqual(result.evidence, (contextual_evidence(self.chunks[0]),))
        wrong = replace(self.records[0], text_version_id="different")
        with self.assertRaises(ValueError):
            restore_context(trace, self.chunks, (wrong,), len)

    def test_restoration_does_not_apply_unagreed_ten_item_policy(self):
        # Given: an archive whose window legitimately restores eleven chunks.
        chunks = tuple(replace(self.chunks[2], chunk_id=f"{i:036d}", ordinal=i,
                               source_turns=(i,)) for i in range(11))
        record = replace(self.records[1], turn_count=11)
        trace = Retrieval("p1", (Hit(chunks[5].chunk_id, 0.9),))
        result = restore_context(trace, chunks, (record,), lambda _: 10,
                                 ContextConfig(window=10))
        self.assertEqual(len(result.evidence), 11)

    def test_exact_float32_search_filters_before_ranking_and_ties_by_uuid(self):
        import numpy as np
        # Given: identical eligible scores and a higher-scoring foreign row.
        chunks = (*self.chunks[:2], replace(self.chunks[2], persona_id="foreign"))
        vectors = np.array([[0.8, 0.6], [0.8, 0.6], [1, 0]], dtype=np.float32)
        query = np.array([1, 0], dtype=np.float32)
        result = exact_search(query, vectors, chunks, "p1")
        self.assertEqual([h.chunk_id for h in result.top5],
                         sorted(c.chunk_id for c in chunks[:2]))
        self.assertEqual([h.score for h in result.top5], [float(np.float32(0.8))] * 2)
        self.assertEqual(exact_search(query, vectors, chunks, "missing").top5, ())

    def test_reproduction_config_pins_retrieval_not_service_model(self):
        data = json.loads((Path(__file__).parents[1] / "configs/persona.json").read_text())
        self.assertEqual(data["retrieval"], {"threshold": THRESHOLD, "k": 3,
                                             "window": 3, "budget": 2000})
        self.assertEqual(data["embedding"]["revision"],
                         "8b418a58414668e75532ed045c22d9ca018ae2b2")
        self.assertEqual(data["budget_tokenizer"]["revision"],
                         "b968826d9c46dd6066d109eabc6255188de91218")
