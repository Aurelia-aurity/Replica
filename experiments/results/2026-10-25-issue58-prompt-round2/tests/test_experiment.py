"""Local artifact tests, not generated model results."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "source"))
sys.path.insert(0, str(HERE / "judge"))
import run_prompt_exp as runner
import check_hashes
import build_blind

spec = importlib.util.spec_from_file_location("issue58_score", HERE / "score.py")
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)


def fixture_rows(round_number=1):
    return [{"model": runner.PROFILE, "prompt": c, "condition": c,
             "suite": i.suite, "case_id": i.case_id, "status": "success",
             "text": "Synthetic test output; not an experiment result.",
             "revision": runner.configuration()["model_revision"],
             "generated_ids_sha256": "0" * 64}
            for c in runner.conditions(round_number) for i in runner.selected(c, runner.load_items())]


class ExperimentTests(unittest.TestCase):
    def test_prompts_when_round_seven_loaded_verbatim(self):
        # Given
        shipped = json.loads((HERE / "source/prompts-r7.json").read_text(encoding="utf-8"))
        # When
        prompts = runner.load_prompts(7)
        # Then
        self.assertEqual(tuple(prompts), ("g7-calm", "g7-elder", "g7-terse", "g7-dialect", "g7-playful"))
        self.assertEqual(prompts, shipped)
        for condition in prompts:
            self.assertEqual(runner.messages(prompts[condition], runner.load_items()[0])[0]["content"],
                             shipped[condition])

    def test_dry_run_when_round_seven_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "7", "--dry-run"]
        items = runner.load_items()
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        # Then
        self.assertEqual(result.stdout.splitlines()[:5], [
            f'{c} {{"human": 20, "probe": 32, "probe_v2": 28}} total=80'
            for c in runner.CONDITIONS_R7])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=400 messages=800 warmup_calls=15 model_loaded=False")
        for condition in runner.conditions(7):
            self.assertEqual(runner.selected(condition, items), runner.selected("style-g4", items))

    def test_blind_and_combined_scores_when_round_seven_generated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r7.jsonl"
            output = Path(directory) / "blind-007"
            rows = fixture_rows(7)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "7",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            loaded = runner.load_generations(path, 7)
            blind, human, key = build_blind.assemble(rows, 5807)
            bids = [next(b for b, k in key.items() if k["condition"] == c) for c in runner.conditions(7)]
            judged = {bid: {"grounding_1to5": 3, "contradiction": False, "premise_accepted": False,
                            "fabricated": False, "core_correct": True, "over_refusal": False,
                            "note": "fixture"} for bid in bids}
            (output / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            scoring = subprocess.run(
                [sys.executable, str(HERE / "score.py"), "--generations-r7", str(path),
                 "--blind-dir-r7", str(output)], capture_output=True, text=True, check=True, timeout=30)
            report = json.loads(scoring.stdout)
            # Then
            self.assertEqual(loaded, rows)
            self.assertEqual(result.stdout.strip(), "BLIND_DONE judge=400 human=400 key=400 seed=5807")
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "human-items.json").read_text()), human)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertEqual({k["condition"] for k in list(key.values())[:80]}, set(runner.conditions(7)))
            self.assertTrue(all(not {"condition", "model", "prompt"} & set(item) for item in blind + human))
            self.assertEqual(report["round7"]["generations"], 400)
            self.assertEqual(report["round7"]["judge_graded"], 5)
            for condition in runner.conditions(7):
                row = report["combined_table"][condition]
                self.assertEqual(row["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(row["grounding_safety_agent"]["count"], 1)
                self.assertEqual(set(row["response_substring_counts"]), set(score.RESPONSE_COUNTERS))
            for invalid in (rows[:-1], rows + rows[:1], rows[:80]):
                path.write_text("".join(json.dumps(row) + "\n" for row in invalid), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "schedule"):
                    runner.load_generations(path, 7)

    def test_habitual_expressions_when_profile_line_parsed(self):
        # Given
        prompt = "어조: ~무관.\n  습관적 어미·표현: ~했지요?, 어., ~하나？, 짜잔…!, ~, , 끝。\n다른 줄: 무관"
        # When
        expressions = score.habitual_expressions(prompt)
        absent = score.habitual_expressions("프로필 없음")
        # Then: punctuation and empty items cannot become match-all substrings.
        self.assertEqual(expressions, ("했지요", "어", "하나", "짜잔", "끝"))
        self.assertEqual(absent, ())

    def test_profile_counts_when_each_condition_has_its_own_habits(self):
        # Given
        rows = fixture_rows(7)
        for row in rows:
            row["text"] = "합성 응답."
        texts = {
            "g7-calm": ("내가 했어.", "천천히 해도 돼.", "해 볼까? 거든 천천히 해도 돼.", "헤헤 짜잔."),
            "g7-elder": ("그랬어요.", "그럼요 그럼요.", "참 괜찮아요.", "천천히 해도 돼."),
            "g7-terse": ("어.", "내가 했다.", "됐어 그래.", "헤헤 짜잔."),
            "g7-dialect": ("같이 했다 아이가.", "내가 했데이.", "함께 하나? 맞나.", "헤헤 짜잔."),
            "g7-playful": ("내가 했지롱.", "헤헤 헤헤.", "내가 했거든? 짜잔.", "그랬어요."),
        }
        for condition, responses in texts.items():
            condition_rows = [r for r in rows if r["condition"] == condition]
            for row, text in zip(condition_rows, responses):
                row["text"] = text
            condition_rows[4].update(status="failed", text=responses[0])
        # When
        table = score.combined_table(score.summarize(rows, {}, {}, 7)["conditions"])
        # Then: suffixes match inside words, multiple/repeated matches count once, foreign profiles do not.
        for condition in runner.conditions(7):
            self.assertEqual(table[condition]["response_substring_counts"]["habitual_any_profile"], 3)

    def test_agree_and_negation_counts_when_all_rounds_summarized(self):
        for round_number in range(1, 8):
            with self.subTest(round_number=round_number):
                # Given
                rows = fixture_rows(round_number)
                for row in rows:
                    row["text"] = "합성 응답."
                for condition in runner.conditions(round_number):
                    condition_rows = [r for r in rows if r["condition"] == condition]
                    for row, text in zip(condition_rows, [
                        "맞아.", " 맞아요.", "\t맞다.", "\n맞제.", "\u3000그럼요.", "그래.", "응.", "어.",
                        " 아니.", "\t아니요.", "\n아이다.", "아닌데.",
                        "앞에 맞아.", "앞에 아니.", '"맞아."', "", " \n\t",
                    ]):
                        row["text"] = text
                    condition_rows[17].update(status="failed", text="맞아.")
                    condition_rows[18].update(status="failed", text="아니.")
                # When
                table = score.combined_table(score.summarize(rows, {}, {}, round_number)["conditions"])
                # Then: prefix counters apply to every condition, including original guarded.
                self.assertEqual(set(table), set(runner.conditions(round_number)))
                for condition in table.values():
                    counters = condition["response_substring_counts"]
                    self.assertEqual(counters["starts_with_agree"], 8)
                    self.assertEqual(counters["starts_with_negation"], 4)
                    self.assertEqual(counters["starts_with_maja"], 1)
                if round_number == 1:
                    self.assertEqual(table["guarded"]["response_substring_counts"]["habitual_any_profile"], 0)

    def test_round_seven_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            summary = score.round7_summary(path / "generations-r7.jsonl", path / "blind-007")
            # Then
            self.assertEqual(summary["status"], "pending_generations")
            self.assertEqual(summary["generations"], 0)
            self.assertEqual(summary["judge_planned"], 400)
            self.assertEqual(set(summary["conditions"]), set(runner.CONDITIONS_R7))
            for condition in summary["conditions"].values():
                self.assertEqual(condition["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(condition["response_substring_counts"], dict.fromkeys(score.RESPONSE_COUNTERS))

    def test_prompts_when_round_six_loaded_verbatim(self):
        # Given
        shipped = json.loads((HERE / "source/prompts-r6.json").read_text(encoding="utf-8"))
        # When
        prompts = runner.load_prompts(6)
        # Then
        self.assertEqual(tuple(prompts), ("style-g6a", "guarded-g6b"))
        self.assertEqual(prompts, shipped)
        for condition in prompts:
            self.assertEqual(runner.messages(prompts[condition], runner.load_items()[0])[0]["content"],
                             shipped[condition])

    def test_dry_run_when_round_six_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "6", "--dry-run"]
        items = runner.load_items()
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        # Then
        self.assertEqual(result.stdout.splitlines()[:2], [
            'style-g6a {"human": 20, "probe": 32, "probe_v2": 28} total=80',
            'guarded-g6b {"human": 20, "probe": 32, "probe_v2": 28} total=80'])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=160 messages=320 warmup_calls=6 model_loaded=False")
        for condition in runner.conditions(6):
            self.assertEqual(runner.selected(condition, items), runner.selected("style-g4", items))

    def test_blind_and_combined_scores_when_round_six_generated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r6.jsonl"
            output = Path(directory) / "blind-006"
            rows = fixture_rows(6)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "6",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            loaded = runner.load_generations(path, 6)
            blind, human, key = build_blind.assemble(rows, 5806)
            bids = [next(b for b, k in key.items() if k["condition"] == c) for c in runner.conditions(6)]
            judged = {bid: {"grounding_1to5": 3, "contradiction": False, "premise_accepted": False,
                            "fabricated": False, "core_correct": True, "over_refusal": False,
                            "note": "fixture"} for bid in bids}
            (output / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            scoring = subprocess.run(
                [sys.executable, str(HERE / "score.py"), "--generations-r6", str(path),
                 "--blind-dir-r6", str(output)], capture_output=True, text=True, check=True, timeout=30)
            report = json.loads(scoring.stdout)
            # Then
            self.assertEqual(loaded, rows)
            self.assertEqual(result.stdout.strip(), "BLIND_DONE judge=160 human=160 key=160 seed=5806")
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "human-items.json").read_text()), human)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertEqual({k["condition"] for k in list(key.values())[:80]}, set(runner.conditions(6)))
            self.assertEqual(report["round6"]["generations"], 160)
            self.assertEqual(report["round6"]["judge_graded"], 2)
            for condition in runner.conditions(6):
                row = report["combined_table"][condition]
                self.assertEqual(row["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(row["grounding_safety_agent"]["count"], 1)
                self.assertEqual(set(row["response_substring_counts"]), set(score.RESPONSE_COUNTERS))
            for invalid in (rows[:-1], rows + rows[:1], rows[:80]):
                path.write_text("".join(json.dumps(row) + "\n" for row in invalid), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "schedule"):
                    runner.load_generations(path, 6)

    def test_habitual_and_prefix_counts_when_all_rounds_summarized(self):
        for round_number in range(1, 8):
            with self.subTest(round_number=round_number):
                # Given
                rows = fixture_rows(round_number)
                for row in rows:
                    row["text"] = "합성 응답."
                for condition in runner.conditions(round_number):
                    condition_rows = [r for r in rows if r["condition"] == condition]
                    for row, text in zip(condition_rows, [
                        "천천히 해도 돼. 하나씩 해 볼까? 천천히 해도 돼. 하나씩 해 볼까?",
                        "괜찮아, 천천히 해도 돼.", "천천히 해도 돼 천천히 해도 돼",
                        "맞아, 천천히 해도 돼.", " 맞아. 천천히 해도돼.",
                    ]):
                        row["text"] = text
                    condition_rows[5].update(status="failed", text="맞아. 천천히 해도 돼.")
                # When
                table = score.combined_table(score.summarize(rows, {}, {}, round_number)["conditions"])
                # Then: include guarded, count each successful response once, and preserve exact spacing.
                self.assertEqual(set(table), set(runner.conditions(round_number)))
                for condition in table.values():
                    counters = condition["response_substring_counts"]
                    self.assertEqual(counters["habitual_phrase"], 1)
                    self.assertEqual(counters["habitual_any"], 4)
                    self.assertEqual(counters["starts_with_maja"], 1)

    def test_round_six_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            summary = score.round6_summary(path / "generations-r6.jsonl", path / "blind-006")
            # Then
            self.assertEqual(summary["status"], "pending_generations")
            self.assertEqual(summary["generations"], 0)
            self.assertEqual(summary["judge_planned"], 160)
            self.assertEqual(set(summary["conditions"]), set(runner.CONDITIONS_R6))
            for condition in summary["conditions"].values():
                self.assertEqual(condition["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(condition["response_substring_counts"], dict.fromkeys(score.RESPONSE_COUNTERS))

    def test_prompts_when_round_five_loaded_verbatim(self):
        # Given
        shipped = json.loads((HERE / "source/prompts-r5.json").read_text(encoding="utf-8"))
        # When
        prompts = runner.load_prompts(5)
        # Then
        self.assertEqual(tuple(prompts), ("style-g5a", "style-g5b"))
        self.assertEqual(prompts, shipped)
        for condition in prompts:
            self.assertEqual(runner.messages(prompts[condition], runner.load_items()[0])[0]["content"],
                             shipped[condition])

    def test_dry_run_when_round_five_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "5", "--dry-run"]
        items = runner.load_items()
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        # Then
        self.assertEqual(result.stdout.splitlines()[:2], [
            'style-g5a {"human": 20, "probe": 32, "probe_v2": 28} total=80',
            'style-g5b {"human": 20, "probe": 32, "probe_v2": 28} total=80'])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=160 messages=320 warmup_calls=6 model_loaded=False")
        for condition in runner.conditions(5):
            self.assertEqual(runner.selected(condition, items), runner.selected("style-g4", items))

    def test_blind_and_scores_when_round_five_generated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r5.jsonl"
            output = Path(directory) / "blind-005"
            rows = fixture_rows(5)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "5",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            loaded = runner.load_generations(path, 5)
            blind, human, key = build_blind.assemble(rows, 5805)
            bids = [next(b for b, k in key.items() if k["condition"] == c) for c in runner.conditions(5)]
            judged = {bid: {"grounding_1to5": 3, "contradiction": False, "premise_accepted": False,
                            "fabricated": False, "core_correct": True, "over_refusal": False,
                            "note": "fixture"} for bid in bids}
            (output / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            summary = score.round5_summary(path, output)
            # Then
            self.assertEqual(loaded, rows)
            self.assertEqual(result.stdout.strip(), "BLIND_DONE judge=160 human=160 key=160 seed=5805")
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "human-items.json").read_text()), human)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertEqual({k["condition"] for k in list(key.values())[:80]}, set(runner.conditions(5)))
            self.assertEqual(summary["judge_planned"], 160)
            self.assertEqual(summary["judge_graded"], 2)
            for condition in score.combined_table(summary["conditions"]).values():
                self.assertEqual(condition["grounding_safety_agent"]["count"], 1)
                self.assertEqual(condition["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(set(condition["response_substring_counts"]), set(score.RESPONSE_COUNTERS))
            for invalid in (rows[:-1], rows + rows[:1], rows[:80]):
                path.write_text("".join(json.dumps(row) + "\n" for row in invalid), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "schedule"):
                    runner.load_generations(path, 5)

    def test_new_counters_when_prefixes_and_foreign_scripts_present(self):
        # Given
        rows = fixture_rows(5)
        for row in rows:
            row["text"] = "한글 응답."
        texts = [
            "맞아. 내 기억엔 그랬어.", " 맞아.", "그건 맞아.", "맞아요.",
            "こんにちは カナ", "半角ｶﾅ", "한글 ㄱ ᄀ ﾡ",
            "기록 Qwen", "기록 中文", "기록 русский", "123!? 😀",
        ]
        for row, text in zip(rows, texts):
            row["text"] = text
        rows[len(texts)].update(status="failed", text="맞아 かな")
        # When
        report = score.combined_table(score.summarize(rows, {}, {}, 5)["conditions"])
        # Then: each response counts once; a literal prefix has no whitespace normalization.
        self.assertEqual(report["style-g5a"]["response_substring_counts"],
                         {"habitual_phrase": 0, "habitual_any": 0, "re_question": 0, "memory": 1,
                          "starts_with_maja": 2, "non_korean_script": 5,
                          "starts_with_agree": 3, "starts_with_negation": 0, "habitual_any_profile": 0})
        self.assertEqual(report["style-g5b"]["response_substring_counts"],
                         dict.fromkeys(score.RESPONSE_COUNTERS, 0))

    def test_round_five_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            summary = score.round5_summary(path / "generations-r5.jsonl", path / "blind-005")
            # Then
            self.assertEqual(summary["status"], "pending_generations")
            self.assertEqual(summary["generations"], 0)
            self.assertEqual(summary["judge_planned"], 160)
            self.assertEqual(set(summary["conditions"]), set(runner.CONDITIONS_R5))
            for condition in summary["conditions"].values():
                self.assertEqual(condition["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(condition["response_substring_counts"], dict.fromkeys(score.RESPONSE_COUNTERS))

    def test_prompt_when_round_four_loaded_verbatim(self):
        # Given
        shipped = json.loads((HERE / "source/prompts-r4.json").read_text(encoding="utf-8"))
        # When
        prompts = runner.load_prompts(4)
        built = runner.messages(prompts["style-g4"], runner.load_items()[0])
        # Then
        self.assertEqual(tuple(prompts), ("style-g4",))
        self.assertEqual(prompts, shipped)
        self.assertEqual(built[0]["content"], shipped["style-g4"])

    def test_dry_run_when_round_four_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "4", "--dry-run"]
        items = runner.load_items()
        expected = [i for i in runner.selected("guarded", items)
                    if i.suite in ("human", "probe", "probe_v2")]
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        selected = runner.selected("style-g4", items)
        # Then
        self.assertEqual(selected, expected)
        self.assertTrue(set(runner.selected("style-g", items)) <= set(selected))
        self.assertEqual(result.stdout.splitlines()[0],
                         'style-g4 {"human": 20, "probe": 32, "probe_v2": 28} total=80')
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=80 messages=160 warmup_calls=3 model_loaded=False")

    def test_blind_and_scores_when_round_four_generated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r4.jsonl"
            output = Path(directory) / "blind-004"
            rows = fixture_rows(4)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "4",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            loaded = runner.load_generations(path, 4)
            blind, human, key = build_blind.assemble(rows, 5804)
            bid = next(iter(key))
            judged = {bid: {"grounding_1to5": 3, "contradiction": False, "premise_accepted": False,
                           "fabricated": False, "core_correct": True, "over_refusal": False, "note": "fixture"}}
            (output / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            summary = score.round4_summary(path, output)
            # Then
            self.assertEqual(loaded, rows)
            self.assertEqual(result.stdout.strip(), "BLIND_DONE judge=80 human=80 key=80 seed=5804")
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "human-items.json").read_text()), human)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertEqual(summary["judge_planned"], 80)
            condition = score.combined_table(summary["conditions"])["style-g4"]
            self.assertEqual(condition["grounding_safety_agent"]["count"], 1)
            self.assertEqual(condition["answerable_core_correct"]["planned"], 8)
            self.assertEqual(condition["grounding_safety_by_type"]["fp_confirm"]["planned"], 8)
            self.assertEqual(condition["grounding_safety_by_type"]["contradiction"]["planned"], 12)
            self.assertEqual(condition["over_refusal"]["planned"], 28)
            with self.assertRaisesRegex(AssertionError, "schedule"):
                runner.load_generations(path, 3)

    def test_substring_counts_when_responses_repeat_or_overlap(self):
        # Given
        rows = fixture_rows()
        for row in rows:
            row["text"] = "합성 응답."
        texts = [
            "천천히 해도 돼. 하나씩 해 볼까? 천천히 해도 돼. 하나씩 해 볼까?",
            "추가로 확인 확인해 줄 더 궁금한 알려 주면 알려줘 내 기억 내 기억",
            "확인해 줄", "더 궁금한", "알려 주면", "알려줘",
            "천천히 해도 돼. 하나씩 해볼까? 내기억",
        ]
        for row, text in zip(rows, texts):
            row["text"] = text
        rows[7].update(status="failed", text="내 기억 알려줘")
        # When
        report = score.combined_table(score.summarize(rows, {}, {})["conditions"])
        # Then: count responses, not occurrences; exact spacing matters.
        self.assertEqual(report["guarded"]["response_substring_counts"],
                         {"habitual_phrase": 1, "habitual_any": 2, "re_question": 5, "memory": 1,
                          "starts_with_maja": 0, "non_korean_script": 0,
                          "starts_with_agree": 0, "starts_with_negation": 0, "habitual_any_profile": 0})
        for condition in runner.CONDITIONS[1:]:
            self.assertEqual(report[condition]["response_substring_counts"],
                             dict.fromkeys(score.RESPONSE_COUNTERS, 0))

    def test_round_four_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            summary = score.round4_summary(path / "generations-r4.jsonl", path / "blind-004")
            # Then
            self.assertEqual(summary["status"], "pending_generations")
            self.assertEqual(summary["generations"], 0)
            self.assertEqual(summary["judge_planned"], 80)
            self.assertEqual(summary["conditions"]["style-g4"]["response_substring_counts"],
                             dict.fromkeys(score.RESPONSE_COUNTERS))

    def test_prompt_composition_when_round_three_loaded(self):
        # Given
        previous = runner.load_prompts()
        profile = previous["style"][len(previous["guarded-v2a"]) + 2:]
        # When
        prompts = runner.load_prompts(3)
        # Then: compare shipped components and the explicit priority contract.
        self.assertEqual(tuple(prompts), runner.CONDITIONS_R3)
        self.assertTrue(prompts["style-g"].startswith(previous["guarded"]))
        self.assertEqual(prompts["style-g"], previous["guarded"] + "\n\n" + profile)
        self.assertEqual(prompts["style-g2"], prompts["style-g"] + "\n" + runner.STYLE_PRIORITY)

    def test_schedule_when_round_three_selected(self):
        # Given
        items = runner.load_items()
        # When
        schedules = {c: runner.selected(c, items) for c in runner.conditions(3)}
        # Then
        self.assertEqual([len(v) for v in schedules.values()], [44, 44])
        for schedule in schedules.values():
            self.assertEqual(schedule, runner.selected("style", items))

    def test_dry_run_when_round_three_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "3", "--dry-run"]
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        # Then
        self.assertEqual(result.stdout.splitlines()[:2], [
            'style-g {"human": 20, "probe": 8, "probe_v2": 16} total=44',
            'style-g2 {"human": 20, "probe": 8, "probe_v2": 16} total=44'])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=88 messages=176 warmup_calls=6 model_loaded=False")

    def test_generations_when_round_three_schedule_validated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r3.jsonl"
            rows = fixture_rows(3)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            # When
            loaded = runner.load_generations(path, 3)
            # Then
            self.assertEqual(loaded, rows)
            for other_round in (1, 2):
                with self.assertRaisesRegex(AssertionError, "schedule"):
                    runner.load_generations(path, other_round)
            for invalid in (rows[:-1], rows + rows[:1]):
                path.write_text("".join(json.dumps(row) + "\n" for row in invalid), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "schedule"):
                    runner.load_generations(path, 3)

    def test_blind_files_when_round_three_built(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r3.jsonl"
            output = Path(directory) / "blind-003"
            rows = fixture_rows(3)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "3",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            # Then
            self.assertEqual({p.name for p in output.iterdir()}, {
                "blind-items.json", "human-items.json", "blind-key-DO-NOT-OPEN.json",
                "target-style.json", "grading-sheet.csv"})
            blind, human, key = build_blind.assemble(rows, 5803)
            self.assertEqual((len(blind), len(human), len(key)), (88, 88, 88))
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "human-items.json").read_text()), human)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertNotEqual(key, build_blind.assemble(rows, 5802)[2])
            self.assertEqual(result.stdout.strip(), "BLIND_DONE judge=88 human=88 key=88 seed=5803")
            repeated = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(repeated.returncode, 0)

    def test_round_three_scores_when_blind_three_grades_present(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r3.jsonl"
            blind_dir = Path(directory) / "blind-003"
            blind_dir.mkdir()
            rows = fixture_rows(3)
            _, _, key = build_blind.assemble(rows, 5803)
            bid = next(b for b, k in key.items() if k["condition"] == "style-g")
            judged = {bid: {"grounding_1to5": 2, "contradiction": True, "premise_accepted": False,
                           "fabricated": False, "core_correct": True, "over_refusal": False, "note": "fixture"}}
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            (blind_dir / "blind-key-DO-NOT-OPEN.json").write_text(json.dumps(key), encoding="utf-8")
            (blind_dir / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            # When
            result = score.round3_summary(path, blind_dir)
            combined = score.combined_table(result["conditions"])
            # Then
            self.assertEqual(result["generations"], 88)
            self.assertEqual(result["judge_planned"], 88)
            self.assertEqual(result["judge_graded"], 1)
            self.assertEqual(combined["style-g"]["contradiction_rate"]["count"], 1)
            self.assertEqual(combined["style-g"]["grounding_safety_agent"]["graded"], 1)
            for condition in combined.values():
                self.assertIsNone(condition["record_qa_slot_score"])
                self.assertEqual(condition["answerable_core_correct"]["planned"], 2)
                self.assertEqual(condition["over_refusal"]["planned"], 22)
                self.assertEqual(condition["grounding_safety_by_type"]["contradiction"]["planned"], 0)

    def test_round_three_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            result = score.round3_summary(path / "generations-r3.jsonl", path / "blind-003")
            # Then
            self.assertEqual(result["status"], "pending_generations")
            self.assertEqual(result["generations"], 0)
            self.assertEqual(result["judge_planned"], 88)
            self.assertEqual(set(result["conditions"]), set(runner.CONDITIONS_R3))
            for condition in result["conditions"].values():
                self.assertEqual(condition["generations"], 0)
                self.assertEqual(condition["failed"], 0)
                self.assertEqual(condition["grounding_safety_agent"]["planned"], 44)
                self.assertIsNone(condition["grounding_safety_agent"]["rate"])
                self.assertIsNone(condition["machine"]["record_qa"])

    def test_combined_report_when_round_three_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            command = [sys.executable, str(HERE / "score.py"),
                       "--generations-r3", str(path / "generations-r3.jsonl"),
                       "--blind-dir-r3", str(path / "blind-003"),
                       "--generations-r5", str(path / "generations-r5.jsonl"),
                       "--blind-dir-r5", str(path / "blind-005"),
                       "--generations-r6", str(path / "generations-r6.jsonl"),
                       "--blind-dir-r6", str(path / "blind-006"),
                       "--generations-r7", str(path / "generations-r7.jsonl"),
                       "--blind-dir-r7", str(path / "blind-007")]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            # Then
            report = json.loads(result.stdout)
            self.assertEqual(report["round3"]["status"], "pending_generations")
            self.assertEqual(set(report["combined_table"]),
                             set(runner.CONDITIONS + runner.CONDITIONS_R2 + runner.CONDITIONS_R3
                                 + runner.CONDITIONS_R4 + runner.CONDITIONS_R5 + runner.CONDITIONS_R6
                                 + runner.CONDITIONS_R7))
            self.assertEqual(report["round7"]["status"], "pending_generations")
            for c in runner.CONDITIONS_R7:
                self.assertEqual(report["combined_table"][c]["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(report["combined_table"][c]["response_substring_counts"],
                                 dict.fromkeys(score.RESPONSE_COUNTERS))
            self.assertEqual(report["round6"]["status"], "pending_generations")
            for c in runner.CONDITIONS_R6:
                self.assertEqual(report["combined_table"][c]["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(report["combined_table"][c]["response_substring_counts"],
                                 dict.fromkeys(score.RESPONSE_COUNTERS))
            self.assertEqual(report["round5"]["status"], "pending_generations")
            for c in runner.CONDITIONS_R5:
                self.assertEqual(report["combined_table"][c]["grounding_safety_agent"]["planned"], 80)
                self.assertEqual(report["combined_table"][c]["response_substring_counts"],
                                 dict.fromkeys(score.RESPONSE_COUNTERS))
            for c in runner.CONDITIONS_R3:
                self.assertEqual(report["combined_table"][c]["grounding_safety_agent"]["planned"], 44)
                self.assertIsNone(report["combined_table"][c]["grounding_safety_agent"]["rate"])

    def test_prompt_composition_when_round_two_loaded(self):
        # Given
        previous = runner.load_prompts()
        # When
        prompts = runner.load_prompts(2)
        # Then: compare shipped components, not a frozen copy of prompt prose.
        self.assertEqual(tuple(prompts), runner.CONDITIONS_R2)
        self.assertTrue(prompts["guarded-v3"].startswith(previous["guarded"] + " "))
        profile = previous["style"][len(previous["guarded-v2a"]):]
        self.assertEqual(prompts["style-v3"], prompts["guarded-v3"] + profile)

    def test_schedule_when_round_two_selected(self):
        # Given
        items = runner.load_items()
        # When
        schedules = {c: runner.selected(c, items) for c in runner.conditions(2)}
        # Then
        self.assertEqual([len(v) for v in schedules.values()], [120, 44])
        self.assertEqual(schedules["guarded-v3"], runner.selected("guarded", items))
        self.assertEqual(schedules["style-v3"], runner.selected("style", items))

    def test_dry_run_when_round_one_is_default(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--dry-run"]
        # When
        default = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        explicit = subprocess.run(command + ["--round", "1"], capture_output=True, text=True,
                                  check=True, timeout=30)
        # Then
        self.assertEqual(default.stdout, explicit.stdout)
        self.assertEqual(default.stdout.splitlines()[:4], [
            'guarded {"human": 20, "instructions": 20, "probe": 32, "probe_v2": 28, "record_qa": 20} total=120',
            'guarded-v2a {"human": 20, "instructions": 20, "probe": 32, "probe_v2": 28, "record_qa": 20} total=120',
            'guarded-v2b {"human": 20, "instructions": 20, "probe": 32, "probe_v2": 28, "record_qa": 20} total=120',
            'style {"human": 20, "probe": 8, "probe_v2": 16} total=44'])
        self.assertEqual(default.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=404 messages=808 warmup_calls=12 model_loaded=False")

    def test_dry_run_when_round_two_selected(self):
        # Given
        command = [sys.executable, str(HERE / "source/run_prompt_exp.py"), "--round", "2", "--dry-run"]
        # When
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        # Then
        self.assertEqual(result.stdout.splitlines()[:2], [
            'guarded-v3 {"human": 20, "instructions": 20, "probe": 32, "probe_v2": 28, "record_qa": 20} total=120',
            'style-v3 {"human": 20, "probe": 8, "probe_v2": 16} total=44'])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "DRY_RUN_DONE generations=164 messages=328 warmup_calls=6 model_loaded=False")

    def test_generations_when_round_two_schedule_validated(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r2.jsonl"
            rows = fixture_rows(2)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            # When
            loaded = runner.load_generations(path, 2)
            # Then
            self.assertEqual(loaded, rows)
            with self.assertRaisesRegex(AssertionError, "schedule"):
                runner.load_generations(path)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows[:-1]), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "schedule"):
                runner.load_generations(path, 2)

    def test_blind_files_when_round_two_built(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r2.jsonl"
            output = Path(directory) / "blind-002"
            rows = fixture_rows(2)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            command = [sys.executable, str(HERE / "judge/build_blind.py"), "--round", "2",
                       "--generations", str(path), "--output", str(output)]
            # When
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            # Then
            self.assertEqual({p.name for p in output.iterdir()}, {
                "blind-items.json", "human-items.json", "blind-key-DO-NOT-OPEN.json",
                "target-style.json", "grading-sheet.csv"})
            blind, human, key = build_blind.assemble(rows, 5802)
            self.assertEqual((len(blind), len(human), len(key)), (124, 124, 124))
            self.assertEqual(json.loads((output / "blind-items.json").read_text()), blind)
            self.assertEqual(json.loads((output / "blind-key-DO-NOT-OPEN.json").read_text()), key)
            self.assertNotEqual(key, build_blind.assemble(rows)[2])
            self.assertEqual(result.stdout.strip(),
                             "BLIND_DONE judge=124 human=124 key=124 seed=5802")
            repeated = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(repeated.returncode, 0)

    def test_round_two_scores_when_blind_two_grades_present(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "generations-r2.jsonl"
            blind_dir = Path(directory) / "blind-002"
            blind_dir.mkdir()
            rows = fixture_rows(2)
            for row in rows:
                if row["suite"] == "record_qa":
                    case = next(json.loads(i.metadata) for i in runner.load_items()
                                if i.suite == row["suite"] and i.case_id == row["case_id"])
                    row["text"] = json.dumps(case["gold"], ensure_ascii=False)
            _, _, key = build_blind.assemble(rows, 5802)
            bid = next(b for b, k in key.items()
                       if k["condition"] == "guarded-v3" and k["case_id"] == "C01")
            judged = {bid: {"grounding_1to5": 2, "contradiction": True, "premise_accepted": False,
                           "fabricated": False, "core_correct": True, "over_refusal": False, "note": "fixture"}}
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            (blind_dir / "blind-key-DO-NOT-OPEN.json").write_text(json.dumps(key), encoding="utf-8")
            (blind_dir / "judged.json").write_text(json.dumps(judged), encoding="utf-8")
            # When
            result = score.round2_summary(path, blind_dir)
            combined = score.combined_table(result["conditions"])
            # Then
            self.assertEqual(result["generations"], 164)
            self.assertEqual(result["judge_planned"], 124)
            self.assertEqual(result["judge_graded"], 1)
            guarded = combined["guarded-v3"]
            self.assertEqual(guarded["contradiction_rate"]["count"], 1)
            self.assertEqual(guarded["grounding_safety_by_type"]["contradiction"]["graded"], 1)
            self.assertEqual(guarded["record_qa_slot_score"]["score"], 1)
            self.assertEqual(guarded["answerable_core_correct"]["planned"], 8)
            self.assertEqual(guarded["over_refusal"]["planned"], 28)
            self.assertIsNone(combined["style-v3"]["record_qa_slot_score"])
            self.assertEqual(set(guarded["grounding_safety_by_type"]), set(score.CASE_TYPES))

    def test_round_two_pending_when_generations_absent(self):
        # Given
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory)
            # When
            result = score.round2_summary(path / "generations-r2.jsonl", path / "blind-002")
            # Then
            self.assertEqual(result["status"], "pending_generations")
            self.assertEqual(result["generations"], 0)
            self.assertEqual(result["judge_planned"], 124)
            for c, planned in (("guarded-v3", 80), ("style-v3", 44)):
                condition = result["conditions"][c]
                self.assertEqual(condition["generations"], 0)
                self.assertEqual(condition["failed"], 0)
                self.assertEqual(condition["grounding_safety_agent"]["planned"], planned)
                self.assertIsNone(condition["grounding_safety_agent"]["rate"])
                self.assertIsNone(condition["machine"]["record_qa"])

    def test_schedule_when_style_subset_selected(self):
        # Given
        items = runner.load_items()
        # When
        schedules = {c: runner.selected(c, items) for c in runner.CONDITIONS}
        # Then
        self.assertEqual([len(v) for v in schedules.values()], [120, 120, 120, 44])
        self.assertEqual(len({(i.suite, i.case_id) for i in schedules["style"]}), 44)
        self.assertTrue({(i.suite, i.case_id) for i in schedules["style"]}
                        <= {(i.suite, i.case_id) for i in schedules["guarded-v2a"]})

    def test_profile_when_configuration_loaded(self):
        # Given / When
        config = runner.configuration()
        # Then
        self.assertEqual(config["model_id"], "Qwen/Qwen3-8B")
        self.assertEqual(config["chat_template_kwargs"], {"enable_thinking": False})
        self.assertFalse(config["do_sample"])
        self.assertEqual(config["max_new_tokens"], 160)

    def test_blinding_when_complete_fixture_supplied(self):
        # Given
        rows = fixture_rows()
        # When
        blind, human, key = build_blind.assemble(rows)
        # Then
        self.assertEqual((len(blind), len(human), len(key)), (284, 284, 284))
        self.assertEqual(len({b["bid"] for b in blind}), 284)
        for item in blind + human:
            self.assertFalse({"model", "condition", "prompt"} & set(item))
        self.assertEqual(build_blind.assemble(rows), (blind, human, key))
        self.assertTrue(all(b["source_response_sha256"] == key[b["bid"]]["source_response_sha256"]
                            for b in blind))

    def test_pending_means_when_no_grades_supplied(self):
        # Given
        rows = fixture_rows()
        # When
        result = score.summarize(rows, {}, {})
        # Then
        self.assertEqual(result["status"], "pending_grades")
        for c in runner.CONDITIONS:
            condition = result["conditions"][c]
            self.assertIsNone(condition["grounding_safety_agent"]["rate"])
            self.assertIsNone(condition["human_means"]["naturalness"]["mean"])
            self.assertEqual(condition["style_matched_44"]["grounding_safety_agent"]["planned"], 44)
        self.assertIsNone(result["conditions"]["style"]["machine"]["record_qa"])
        self.assertEqual(result["conditions"]["guarded"]["machine"]["record_qa"]["whole_accuracy"], 0)

    def test_risk_and_contradiction_when_one_grade_supplied(self):
        # Given
        rows = fixture_rows()
        _, _, key = build_blind.assemble(rows)
        bid = next(b for b, k in key.items() if k["condition"] == "guarded" and k["case_id"] == "C01")
        judge = {bid: {"grounding_1to5": 2, "contradiction": True, "premise_accepted": False,
                       "fabricated": False, "core_correct": False, "over_refusal": True, "note": "fixture"}}
        # When
        result = score.summarize(rows, judge, {})
        # Then
        condition = result["conditions"]["guarded"]
        self.assertEqual(condition["grounding_safety_agent"]["count"], 0)
        self.assertEqual(condition["contradiction_probes_v2"]["count"], 1)
        self.assertEqual(condition["contradiction_probes_v2"]["graded"], 1)
        self.assertEqual(condition["contradiction_probes_v2"]["planned"], 12)
        self.assertFalse(condition["contradiction_probes_v2"]["complete"])

    def test_stale_grade_when_response_hash_differs(self):
        # Given
        rows = fixture_rows()
        _, _, key = build_blind.assemble(rows)
        bid = next(iter(key))
        grades = {bid: {"source_response_sha256": "wrong", **dict.fromkeys(build_blind.HUMAN_FIELDS, "")}}
        # When / Then
        with self.assertRaisesRegex(AssertionError, "Stale grade"):
            score.summarize(rows, {}, grades)

    def test_hash_helper_when_previous_run_compared_to_itself(self):
        # Given
        previous = check_hashes.PREVIOUS
        # When
        result = check_hashes.compare(previous)
        # Then
        self.assertEqual(result["matched"], 92)
        self.assertTrue(result["complete_match"])
        self.assertEqual(result["mismatches"], [])


if __name__ == "__main__":
    unittest.main()
