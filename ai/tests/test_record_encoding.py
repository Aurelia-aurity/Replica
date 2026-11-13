"""Synthetic boundary doubles; none of these tests executes KURE weights."""
from dataclasses import FrozenInstanceError, replace
import importlib
import itertools
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from encoding_types import EncodingError, EncodingLimits, SourceRecord, SourceUnit
from record_chunking import chunk_record
from record_encoding import EncodingCandidate, LocalEncoder, read_candidate
from record_encoding_doubles import ModelBoundary


def record(text="text", **changes):
    source = SourceRecord(UUID(int=1), UUID(int=2), UUID(int=3), 1, 7, "diary",
                          (SourceUnit(text, "speaker", "persona", "2026-11-12"),))
    return replace(source, **changes)


def reconstruct(source, drafts):
    output = [[] for _ in source.units]
    cursor = [0] * len(source.units)
    for draft in drafts:
        pieces = []
        for span in draft.spans:
            assert span.start == cursor[span.unit_index]
            assert span.end > span.start
            part = source.units[span.unit_index].text[span.start:span.end]
            output[span.unit_index].append(part)
            pieces.append(part)
            cursor[span.unit_index] = span.end
        assert draft.content == "".join(pieces)
        assert draft.source == source
    return tuple("".join(parts) for parts in output)


class ChunkTests(unittest.TestCase):
    def test_unicode_span_roundtrip_finite_domain(self):
        for length in range(1, 5):
            for parts in itertools.product(("한", " ", "e\u0301", "😀"), repeat=length):
                source = record("".join(parts))
                drafts = chunk_record(source, lambda text: 509 + len(text.split("\n", 1)[1]))
                self.assertEqual(reconstruct(source, drafts), (source.units[0].text,))
                self.assertEqual(drafts, chunk_record(source, lambda text: 509 + len(text.split("\n", 1)[1])))

    def test_paragraphs_preserve_blank_lines_and_whitespace(self):
        source = record("  first\n\n \nsecond  \n\n")
        drafts = chunk_record(source, lambda _: 20)
        self.assertEqual([d.content for d in drafts], ["  first\n\n", " \nsecond  \n\n"])
        self.assertEqual(reconstruct(source, drafts), (source.units[0].text,))

    def test_chat_groups_two_adjacent_matching_turns_only(self):
        units = tuple(SourceUnit(text, "나", role, date, turn) for text, role, date, turn in (
            (" a ", "provider", "2026-11-12", 2), ("b\n", "provider", "2026-11-12", 3),
            ("c", "provider", "2026-11-12", 4), ("d", "persona", "2026-11-12", 5),
            ("e", "persona", "2026-11-13", 6), ("f", "persona", "2026-11-13", 8)))
        source = record(kind="chat", units=units)
        drafts = chunk_record(source, lambda _: 1)
        self.assertEqual([d.source_turns for d in drafts], [(2, 3), (4,), (5,), (6,), (8,)])
        self.assertEqual([d.role for d in drafts], ["provider", "provider", "persona", "persona", "persona"])
        self.assertEqual(reconstruct(source, drafts), tuple(u.text for u in units))

    def test_chat_rendering_keeps_turn_boundaries_separate_from_source_content(self):
        source = record(kind="chat", units=(
            SourceUnit("아니", "speaker", "persona", "2026-11-12", 1),
            SourceUnit("맞아", "speaker", "persona", "2026-11-12", 2)))
        drafts = chunk_record(source, lambda _: 1)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].content, "아니맞아")
        self.assertEqual(reconstruct(source, drafts), ("아니", "맞아"))
        self.assertEqual(drafts[0].rendered(), "2026-11-12 | speaker (persona)\n아니\n맞아")

    def test_nonmonotone_prefix_and_backtracking(self):
        source = record("abcde")
        # abc fits but de cannot; ab+cde is feasible. Single characters never fit.
        drafts = chunk_record(source, lambda text: 20 if text.split("\n", 1)[1] in
                              ("ab", "abc", "cde") else 513)
        self.assertEqual([d.content for d in drafts], ["ab", "cde"])

    def test_long_input_splits_losslessly_with_default_work_budget(self):
        source = record(" 한😀e\u0301 " * 1200)
        drafts = chunk_record(source, lambda text: len(text) + 2)
        self.assertEqual(reconstruct(source, drafts), (source.units[0].text,))
        self.assertEqual([d.ordinal for d in drafts], list(range(len(drafts))))
        self.assertTrue(all(len(d.rendered()) + 2 <= 512 for d in drafts))

    def test_preferred_paragraph_and_pair_boundaries_are_backtrackable(self):
        diary = record("a\n\nb")
        drafts = chunk_record(diary, lambda text: 2 if text.endswith("a\n\nb") else 513)
        self.assertEqual([d.content for d in drafts], ["a\n\nb"])
        units = tuple(SourceUnit(c, "speaker", "persona", "2026-11-12", n)
                      for n, c in enumerate("abc"))
        chat = record(kind="chat", units=units)
        drafts = chunk_record(chat, lambda text: 2 if text.split("\n", 1)[1] in
                              ("a\nb", "a", "b\nc") else 513)
        self.assertEqual([d.content for d in drafts], ["a", "bc"])

    def test_metadata_and_special_tokens_exact_boundary(self):
        overhead = len("2026-11-12 | speaker (persona)\n") + 2
        source = record("x" * (512 - overhead))
        count = lambda text: len(text) + 2
        self.assertEqual(len(chunk_record(source, count)), 1)
        longer = replace(source, units=(replace(source.units[0], text=source.units[0].text + "x"),))
        drafts = chunk_record(longer, count)
        self.assertEqual(len(drafts), 2)
        self.assertTrue(all(count(d.rendered()) <= 512 for d in drafts))

    def test_blank_retained_empty_rejected(self):
        source = record(" \t\n")
        self.assertEqual(reconstruct(source, chunk_record(source, len)), (" \t\n",))
        for create in (lambda: record(""), lambda: record(units=())):
            with self.assertRaises(EncodingError):
                create()

    def test_resource_limits_fail_explicitly(self):
        for limits, code in (
            (EncodingLimits(max_codepoints=2), "input_budget"),
            (EncodingLimits(max_token_calls=1), "token_work_budget"),
            (EncodingLimits(max_rendered_codepoints=1), "token_work_budget"),
            (EncodingLimits(max_chunks=1), "chunk_budget"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(EncodingError, code):
                chunk_record(record("abcd"), lambda text: 510 + len(text.split("\n", 1)[1]), limits)
        with self.assertRaisesRegex(EncodingError, "unencodable_source"):
            chunk_record(record("abc"), lambda _: 513)
        for count in (lambda _: -1, lambda _: True):
            with self.assertRaisesRegex(EncodingError, "invalid_token_count"):
                chunk_record(record("abc"), count)

    def test_input_revision_role_date_turn_and_immutability(self):
        for change in ({"revision": 0}, {"source_revision": -1}, {"record_id": "fake"},
                       {"kind": "text"}, {"units": []}):
            with self.subTest(change=change), self.assertRaises(EncodingError):
                record(**change)
        for change in ({"role": "guessed"}, {"date": "yesterday"}, {"turn": -1}):
            with self.subTest(change=change), self.assertRaises(EncodingError):
                replace(record().units[0], **change)
        for turns in ((None,), (2, 2), (3, 1)):
            units = tuple(SourceUnit("x", "speaker", "persona", "2026-11-12", n) for n in turns)
            with self.subTest(turns=turns), self.assertRaises(EncodingError):
                record(kind="chat", units=units)
        with self.assertRaises(FrozenInstanceError):
            record().revision = 2


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "modules.json").write_text(json.dumps([
            {"type": "sentence_transformers.models.Transformer", "path": ""},
            {"type": "sentence_transformers.models.Pooling", "path": "1_Pooling"}]))
        self.model = ModelBoundary()
        module = ModuleType("sentence_transformers")
        module.__version__ = "5.1.2"

        def load(path, **kwargs):
            expected = dict(device="cpu", local_files_only=True, trust_remote_code=False,
                            token=False, backend="torch")
            expected.update({name: dict(local_files_only=True, trust_remote_code=False)
                             for name in ("model_kwargs", "tokenizer_kwargs", "config_kwargs")})
            expected["model_kwargs"]["use_safetensors"] = True
            if Path(path) != self.path.resolve() or kwargs != expected:
                raise EncodingError("unsafe_loader_policy")
            return self.model
        module.SentenceTransformer = load
        self.runtime = patch.dict(sys.modules, {"sentence_transformers": module})
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def test_document_query_real_render_normalization_and_order(self):
        encoder = LocalEncoder(self.path)
        sources = (record("one"), record("two", revision=2, text_version_id=UUID(int=4),
                                       persona_id=UUID(int=5)))
        result = encoder.encode_records(sources)
        self.assertEqual([d.source.revision for d in result.drafts], [1, 2])
        self.assertEqual([d.source.persona_id for d in result.drafts], [UUID(int=3), UUID(int=5)])
        self.assertEqual(self.model.seen, ["2026-11-12 | speaker (persona)\none",
                                          "2026-11-12 | speaker (persona)\ntwo"])
        self.assertEqual(result.rows[0][:2], (struct.unpack("f", struct.pack("f", .6))[0],
                                            struct.unpack("f", struct.pack("f", .8))[0]))
        self.assertNotEqual(result.rows[0], result.rows[1])
        self.assertTrue(all(math.isclose(math.hypot(*row), 1, rel_tol=1e-6) for row in result.rows))
        queries = encoder.encode_queries((" q1 ", "q2"))
        self.assertEqual(self.model.seen[-2:], [" q1 ", "q2"])
        self.assertEqual(len(queries.rows), 2)
        self.assertIsNone(result.provenance.actual_revision)
        self.assertEqual(result.provenance.identity_status, "unverified_local_directory")

    def test_vector_shape_nonfinite_overflow_underflow_and_zero_rejected(self):
        encoder = LocalEncoder(self.path)
        for rows in ([], [1.0], [[1.0]], [[0.0] * 1024], [[float("nan")] * 1024],
                     [[float("inf")] * 1024], [[1e100] * 1024], [[1e-100] * 1024],
                     [[True] * 1024]):
            self.model.row_override = rows
            with self.subTest(first=repr(rows)[:40]), self.assertRaises(EncodingError):
                encoder.encode_queries(("q",))

    def test_query_ceiling_special_tokens_and_input_budget(self):
        encoder = LocalEncoder(self.path)
        self.assertEqual(len(encoder.encode_queries(("x" * 510,)).rows), 1)
        with self.assertRaisesRegex(EncodingError, "encoder_token_limit"):
            encoder.encode_queries(("x" * 511,))
        with self.assertRaisesRegex(EncodingError, "input_budget"):
            encoder.encode_queries(("abc",), EncodingLimits(max_codepoints=2))

    def test_raw_and_actual_strip_lower_token_counts(self):
        encoder = LocalEncoder(self.path)
        self.model.do_lower_case = True
        self.model.tokenizer = lambda text, **kwargs: {"input_ids": [0] * (513 if text == "x" else 2)}
        with self.assertRaisesRegex(EncodingError, "encoder_token_limit"):
            encoder.encode_queries((" X ",))

    def test_missing_path_manifest_dependency_and_wrong_dimension(self):
        with self.assertRaisesRegex(EncodingError, "local_model_directory_required"):
            LocalEncoder(self.path / "missing")
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaisesRegex(EncodingError, "encoding_dependencies_unavailable"):
                LocalEncoder(self.path)
        with patch.object(sys.modules["sentence_transformers"], "__version__", "other"):
            with self.assertRaisesRegex(EncodingError, "unsupported_encoding_runtime"):
                LocalEncoder(self.path)
        (self.path / "config_sentence_transformers.json").write_text('{"model_type":"SparseEncoder"}')
        with self.assertRaisesRegex(EncodingError, "invalid_local_model_type"):
            LocalEncoder(self.path)
        (self.path / "config_sentence_transformers.json").write_text("{}")
        self.model.get_sentence_embedding_dimension = lambda: 384
        with self.assertRaisesRegex(EncodingError, "model_dimension_mismatch"):
            LocalEncoder(self.path)
        (self.path / "modules.json").write_text("[]")
        with self.assertRaisesRegex(EncodingError, "unsupported_local_modules"):
            LocalEncoder(self.path)
        for manifest in (
            [1], [{"type": "custom.Remote", "path": ""}],
            [{"type": "sentence_transformers.models.Transformer", "path": "../outside"}],
        ):
            (self.path / "modules.json").write_text(json.dumps(manifest))
            with self.subTest(manifest=manifest), self.assertRaises(EncodingError):
                LocalEncoder(self.path)

    def test_candidate_config_and_dependency_free_module_import(self):
        path = Path(__file__).resolve().parents[1] / "configs" / "encoding-candidate.json"
        self.assertEqual(read_candidate(path), EncodingCandidate())
        with patch.dict(sys.modules, {"sentence_transformers": None, "torch": None, "numpy": None}):
            spec = importlib.util.spec_from_file_location(
                "encoding_import_probe", path.parents[1] / "src" / "record_encoding.py")
            probe = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"encoding_import_probe": probe}):
                spec.loader.exec_module(probe)
            self.assertEqual(probe.EncodingCandidate().dimensions, 1024)
