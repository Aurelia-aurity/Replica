import sys
import unittest
import io
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import observation_clock as oc


class ObservationClock(unittest.TestCase):
    def verify_fixed_time(self, evidence):
        """Use the same fresh snapshot and local-clock samples for each control."""
        base = datetime(2026, 10, 10, 12, 0, 10, tzinfo=timezone.utc)
        mark = oc.SnapshotMark(100_000_000_000, base)
        mono = iter((100_000_000_000, 101_000_000_000))
        wall = iter((base, base + timedelta(seconds=1)))
        return oc.verify_snapshot(mark, lambda: evidence,
                                  monotonic_ns=lambda: next(mono),
                                  utc_now=lambda: next(wall))

    @staticmethod
    def fresh_evidence():
        return {"date": "Sat, 10 Oct 2026 12:00:10 GMT",
                "cache_control": "no-cache", "age": None, "x_cache": None}

    def test_fresh_date_is_projected_to_snapshot_and_charged_for_rtt_age(self):
        base = datetime(2026, 10, 10, 12, 0, 10, tzinfo=timezone.utc)
        mono = iter((10_000_000_000, 11_000_000_000, 12_000_000_000))
        wall = iter((base, base + timedelta(seconds=1), base + timedelta(seconds=2)))
        mark = oc.mark_snapshot(monotonic_ns=lambda: next(mono), utc_now=lambda: next(wall))
        date = "Sat, 10 Oct 2026 12:00:12 GMT"
        result = oc.verify_snapshot(mark, lambda: {"date": date, "cache_control": "no-cache",
                                                   "age": "0", "x_cache": None},
                                   monotonic_ns=lambda: next(mono), utc_now=lambda: next(wall))
        self.assertEqual(result, ("2026-10-10T12:00:10.500000Z", 4))

    def test_stale_sample_and_slow_or_jumping_local_clock_are_unknown(self):
        base = datetime(2026, 10, 10, 12, 0, 10, tzinfo=timezone.utc)
        mark = oc.SnapshotMark(100_000_000_000, base)
        mono = iter((100_000_000_000, 101_000_000_000))
        wall = iter((base + timedelta(seconds=99), base + timedelta(seconds=100)))
        self.assertEqual(oc.verify_snapshot(mark, lambda: {"date": "Sat, 10 Oct 2026 12:01:49 GMT",
                                                            "cache_control": "no-cache",
                                                            "age": "0", "x_cache": None},
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))
        mono = iter((100_000_000_000, 101_000_000_000))
        wall = iter((base + timedelta(seconds=1), base + timedelta(seconds=9)))
        self.assertEqual(oc.verify_snapshot(mark, lambda: {"date": "Sat, 10 Oct 2026 12:00:11 GMT",
                                                            "cache_control": "no-cache",
                                                            "age": "0", "x_cache": None},
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))

    def test_invalid_or_low_precision_date_does_not_claim_time(self):
        valid = self.fresh_evidence()
        self.assertNotEqual(self.verify_fixed_time(valid), (None, None))
        self.assertEqual(self.verify_fixed_time(None), (None, None))
        invalid = {**valid, "date": "2026-10-10T12:00:00Z"}
        self.assertEqual(self.verify_fixed_time(invalid), (None, None))

    def test_freshness_rejections_share_a_passing_positive_control(self):
        valid = self.fresh_evidence()
        positive = self.verify_fixed_time(valid)
        self.assertIsInstance(positive[0], str)
        self.assertIsInstance(positive[1], int)
        self.assertLessEqual(positive[1], oc.MAX_UNCERTAINTY_SECONDS)

        bad_evidence = (
            ("Age", {**valid, "age": "2"}),
            ("X-Cache", {**valid, "x_cache": "HIT"}),
            ("Cache-Control", {**valid, "cache_control": "private, max-age=60"}),
            ("Date format", {**valid, "date": "2026-10-10T12:00:10Z"}),
            ("stale Date", {**valid, "date": "Sat, 10 Oct 2026 12:02:10 GMT"}),
        )
        for evidence_name, evidence in bad_evidence:
            with self.subTest(evidence=evidence_name):
                # With the same current mark/RTT/local clock as the passing
                # control, rejection must come from the mutated evidence.
                self.assertEqual(self.verify_fixed_time(evidence), (None, None))

    def test_missing_cache_directives_and_clock_evidence_gaps_fail_same_time_control(self):
        valid = self.fresh_evidence()
        positive = self.verify_fixed_time(valid)
        self.assertIsInstance(positive[0], str)
        self.assertLessEqual(positive[1], oc.MAX_UNCERTAINTY_SECONDS)
        rejected = {
            "missing Cache-Control": {**valid, "cache_control": None},
            "max-age without no-cache": {**valid, "cache_control": "max-age=60"},
            "missing freshness evidence field": {key: value for key, value in valid.items()
                                                if key != "cache_control"},
        }
        for case, evidence in rejected.items():
            with self.subTest(case=case):
                # Same mark, RTT, wall-clock samples, and valid Date as the
                # positive control; only the named freshness evidence differs.
                self.assertEqual(self.verify_fixed_time(evidence), (None, None))

    def test_monotonic_reversal_long_rtt_and_backward_wall_jump_are_unknown(self):
        base = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        evidence = {"date": "Sat, 10 Oct 2026 12:00:00 GMT",
                    "cache_control": "no-cache", "age": "0", "x_cache": None}

        mark = oc.SnapshotMark(100_000_000_000, base)
        mono = iter((100_000_000_000, 99_000_000_000))
        wall = iter((base, base))
        self.assertEqual(oc.verify_snapshot(mark, lambda: evidence,
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))

        # A 122-second RTT remains over the 60-second uncertainty ceiling even
        # when its wall-clock samples and server Date are otherwise consistent.
        mark = oc.SnapshotMark(0, base)
        mono = iter((60_000_000_000, 182_000_000_000))
        wall = iter((base + timedelta(seconds=60), base + timedelta(seconds=182)))
        long_rtt_evidence = {**evidence, "date": "Sat, 10 Oct 2026 12:02:01 GMT"}
        self.assertEqual(oc.verify_snapshot(mark, lambda: long_rtt_evidence,
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))

        mark = oc.SnapshotMark(100_000_000_000, base)
        mono = iter((100_000_000_000, 101_000_000_000))
        wall = iter((base, base - timedelta(seconds=2)))
        self.assertEqual(oc.verify_snapshot(mark, lambda: evidence,
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))

    def test_http_date_round_trip_uses_locale_independent_english_format(self):
        value = "Sat, 10 Oct 2026 12:00:00 GMT"
        parsed = oc._parse_date(value)
        self.assertEqual(parsed, datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(oc.format_datetime(parsed, usegmt=True), value)

    def test_cache_age_and_missing_freshness_evidence_are_unknown(self):
        valid = self.fresh_evidence()
        self.assertNotEqual(self.verify_fixed_time(valid), (None, None))
        for evidence in ({**valid, "age": "2", "x_cache": "MISS"},
                         {**valid, "cache_control": "private, max-age=60"},
                         {**valid, "x_cache": "HIT"}):
            self.assertEqual(self.verify_fixed_time(evidence), (None, None))

    def test_live_header_shape_no_cache_date_without_age_or_x_cache_is_accepted(self):
        base = datetime(2026, 10, 10, 12, 0, 10, tzinfo=timezone.utc)
        mono = iter((10_000_000_000, 11_000_000_000, 12_000_000_000))
        wall = iter((base, base + timedelta(seconds=1), base + timedelta(seconds=2)))
        mark = oc.mark_snapshot(monotonic_ns=lambda: next(mono), utc_now=lambda: next(wall))
        headers = {"date": "Sat, 10 Oct 2026 12:00:12 GMT",
                   "cache_control": "no-cache", "age": None, "x_cache": None}
        result = oc.verify_snapshot(mark, lambda: headers,
                                   monotonic_ns=lambda: next(mono), utc_now=lambda: next(wall))
        self.assertEqual(result, ("2026-10-10T12:00:10.500000Z", 4))

    def test_negative_delta_preserves_fractional_estimate_at_output_boundary(self):
        base = datetime(2026, 10, 10, 12, 0, 0, 950_000, tzinfo=timezone.utc)
        mark = oc.SnapshotMark(10_000_000_000, base)
        mono = iter((10_040_000_000, 10_060_000_000))
        wall = iter((base + timedelta(milliseconds=40),
                     base + timedelta(milliseconds=60)))
        result = oc.verify_snapshot(
            mark,
            lambda: {"date": "Sat, 10 Oct 2026 12:00:01 GMT",
                     "cache_control": "no-cache", "age": None, "x_cache": None},
            monotonic_ns=lambda: next(mono), utc_now=lambda: next(wall))
        # The midpoint is 50 ms after the mark, so projecting the second-
        # precision Date back yields .950. Truncating this to .000 would add
        # nearly a second not represented by the computed uncertainty.
        self.assertEqual(result, ("2026-10-10T12:00:00.950000Z", 2))

    def test_stale_date_outside_bound_is_unknown(self):
        base = datetime(2026, 10, 10, 12, 2, 0, tzinfo=timezone.utc)
        mark = oc.SnapshotMark(100_000_000_000, base)
        mono = iter((100_000_000_000, 101_000_000_000))
        wall = iter((base, base + timedelta(seconds=1)))
        stale = {"date": "Sat, 10 Oct 2026 12:00:58 GMT", "cache_control": "no-cache",
                 "age": None, "x_cache": None}
        self.assertEqual(oc.verify_snapshot(mark, lambda: stale,
                                            monotonic_ns=lambda: next(mono),
                                            utc_now=lambda: next(wall)), (None, None))

    def test_dedicated_fetch_uses_no_cache_nonce_and_requires_cache_evidence(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Date": "Sat, 10 Oct 2026 12:00:00 GMT", "Cache-Control": "no-cache"}
            def geturl(self): return "https://api.github.com/rate_limit"
        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response(b"{}")
        opener = Opener()
        client = type("Client", (), {"headers": {"Authorization": "Bearer test-only"},
                                     "opener": opener})()
        evidence = oc.fetch_fresh_date(client)
        self.assertEqual(evidence["cache_control"], "no-cache")
        self.assertEqual(opener.timeout, 10)
        self.assertEqual(opener.request.get_header("Cache-control"), "no-cache, no-store, max-age=0")
        self.assertIn("/rate_limit?_clock_nonce=", opener.request.full_url)

        Response.headers = {"Date": "Sat, 10 Oct 2026 12:00:00 GMT",
                            "Cache-Control": "no-cache", "Age": "3"}
        evidence = oc.fetch_fresh_date(client)
        self.assertEqual(evidence["age"], "3")
        reading = oc.verify_snapshot(oc.SnapshotMark(1, datetime.now(timezone.utc)), lambda: evidence)
        self.assertEqual(reading, (None, None))


if __name__ == "__main__":
    unittest.main()
