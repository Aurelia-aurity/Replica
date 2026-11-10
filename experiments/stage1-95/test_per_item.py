"""Real cosine search and v2 restoration on synthetic, gold-free inputs."""

import numpy as np
import pytest

from corpus import chunk_corpus
from per_item import ItemQuery, RetrievalPolicy, SearchIndex, retrieve_items, split_items
from retrieval_v2 import ContextConfig, evidence_json


def setup_index(corpus):
    chunks = chunk_corpus(corpus)
    # Two target-persona records with orthogonal query intent and a foreign hit.
    vectors = np.array([[1, 0], [1, 0], [0, 1], [0, 1], [1, 1], [1, 1]], dtype=np.float32)
    return SearchIndex(chunks, corpus.records, vectors)


def test_split_when_explicit_items_and_shared_context():
    # Given: a two-clause question without any answers or support annotations.
    question = "첫 사건은 어디였어; 다음 사건은 무엇을 썼어?"
    # When: split on explicit clause boundaries.
    queries = split_items(question)
    # Then: both clauses retain original context, with distinct item focus.
    assert len(queries) == 2
    assert all(query.startswith(question) for query in queries)
    assert queries[0].endswith("첫 사건은 어디였어")
    assert queries[1].endswith("다음 사건은 무엇을 썼어?")


def test_split_when_no_supported_separator():
    # Given / When: unmarked Korean shared-predicate syntax is not guessed.
    question = "오전과 오후에 각각 어디서 무엇을 썼어?"
    # Then: explicit authored plans are needed for this construction.
    assert split_items(question) == (question,)


def test_retrieval_when_per_item_hits_overlap(synthetic_corpus):
    # Given: real exact-search vectors, including a foreign record with a high score.
    index = setup_index(synthetic_corpus)
    queries = (ItemQuery("first", np.array([1, 0], dtype=np.float32)),
               ItemQuery("second", np.array([0, 1], dtype=np.float32)))
    policy = RetrievalPolicy(len)
    # When: run v2 once for each item and compose under the original global cap.
    result = retrieve_items(synthetic_corpus.questions[0], queries, index, policy=policy)
    # Then: canonical sources appear once, never from the foreign persona.
    assert {e.record_id.int for e in result.evidence} == {1, 2}
    assert len(result.evidence) == len({e.chunk_id for e in result.evidence})
    assert len(evidence_json(result.evidence)) <= 2000
    assert all(item.retained_ids for item in result.items)


def test_retrieval_when_budget_only_retains_one_record(synthetic_corpus):
    # Given: each item finds a different source, but only one group can fit.
    index = setup_index(synthetic_corpus)
    queries = (ItemQuery("first", np.array([1, 0], dtype=np.float32)),
               ItemQuery("second", np.array([0, 1], dtype=np.float32)))
    # Count whole record groups, independent of JSON bytes.
    def count(text):
        return 2 + text.count('"chunk_id"') * 100
    policy = RetrievalPolicy(count, ContextConfig(k=1, window=3, budget=202))
    # When: global composition has the same budget, not one full cap per item.
    result = retrieve_items(synthetic_corpus.questions[0], queries, index, policy=policy)
    # Then: the second item explicitly reports retrieval lost during merge.
    assert count(evidence_json(result.evidence)) <= 202
    assert result.items[0].retained_ids
    assert result.items[1].retrieved_ids and not result.items[1].retained_ids


def test_retrieval_when_all_queries_abstain(synthetic_corpus):
    # Given: every eligible score is below the fixed v2 threshold.
    index = setup_index(synthetic_corpus)
    queries = (ItemQuery("unknown", np.array([-1, -1], dtype=np.float32)),)
    # When: v2 abstains at the item level.
    result = retrieve_items(synthetic_corpus.questions[0], queries, index, policy=RetrievalPolicy(len))
    # Then: merging cannot resurrect unaccepted top-k evidence.
    assert result.evidence == ()
    assert result.items[0].abstained and not result.items[0].retained_ids


def test_retrieval_when_gold_labels_change(synthetic_corpus):
    # Given: retrieval inputs are identical, but all answer metadata changes.
    index = setup_index(synthetic_corpus)
    question = synthetic_corpus.questions[0]
    changed = question.model_copy(update={
        "required_slots": ("invented",), "expected_answer": "not an input",
        "gold_chunk_ids": (index.chunks[-1].chunk_id,), "type": "unanswerable",
    })
    queries = (ItemQuery("first", np.array([1, 0], dtype=np.float32)),)
    # When: retrieve through the same real v2 function.
    first = retrieve_items(question, queries, index, policy=RetrievalPolicy(len))
    second = retrieve_items(changed, queries, index, policy=RetrievalPolicy(len))
    # Then: gold metadata cannot affect selection or its item trace.
    assert first == second


def test_retrieval_when_nondev_input(synthetic_corpus):
    # Given: a synthetic changed split, without opening any non-DEV file.
    question = synthetic_corpus.questions[0].model_copy(update={"split": "heldout"})
    # When / Then: fail before search.
    with pytest.raises(ValueError):
        retrieve_items(question, (ItemQuery("x", np.array([1, 0], dtype=np.float32)),),
                       setup_index(synthetic_corpus), policy=RetrievalPolicy(len))
