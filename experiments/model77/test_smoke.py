"""Stdlib tests for structural template and generation policy."""

import unittest

from smoke import CAP, empty_think_suffix, load_profile, termination, think_markers


class SmokeTests(unittest.TestCase):
    def test_empty_block_at_generation_boundary(self):
        self.assertTrue(empty_think_suffix("assistant\n<think>\n\n</think>\n"))

    def test_nonempty_block_rejected(self):
        self.assertFalse(empty_think_suffix("<think>x</think>"))

    def test_open_block_rejected(self):
        self.assertFalse(empty_think_suffix("<think>\n"))

    def test_block_before_answer_rejected(self):
        self.assertFalse(empty_think_suffix("<think></think>x"))

    def test_raw_special_markers_detected(self):
        self.assertEqual(think_markers("<think>x</think>"), ["<think>", "</think>"])

    def test_plain_answer_has_no_markers(self):
        self.assertEqual(think_markers("abc"), [])

    def test_eos_list_uses_all_ids_at_cap(self):
        self.assertEqual(termination([0] * (CAP - 1) + [9], [8, 9]), "eos")

    def test_length_without_eos(self):
        self.assertEqual(termination([0] * CAP, [8, 9]), "length")

    def test_short_generation_without_eos(self):
        self.assertEqual(termination([0], [8]), "other")

    def test_kanana_profile_selects_own_template_switch(self):
        profile = load_profile("kanana-2-3b-instruct")
        self.assertEqual(profile.chat_template_kwargs, {"thinking_mode": "no_think"})

    def test_unknown_profile_fails_closed(self):
        with self.assertRaises(KeyError):
            load_profile("unknown")


if __name__ == "__main__":
    unittest.main()
