"""Prepared offline runner. Requires a future explicit freeze and GPU permission."""

from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from functools import wraps
from io import StringIO
from hashlib import sha256
from inspect import signature
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import TextIO


if __name__ == "__main__":
    # Only this executable owns the diagnostic policy; library imports do not.
    def report_uncaught(error_type, error, traceback):
        if issubclass(error_type, Exception):
            print("Cannot initialize experiment runner; check the training interpreter and required dependencies.", file=sys.stderr)
        elif issubclass(error_type, KeyboardInterrupt):
            print("Interrupted.", file=sys.stderr)
        else:
            sys.__excepthook__(error_type, error, traceback)

    sys.excepthook = report_uncaught


try:
    import click
    from pydantic import Field, TypeAdapter
    import typer

    from corpus import Evidence, FrozenModel
    from generate import EVIDENCE_END, EVIDENCE_START
    from prepare100 import HERE, ROOT, reject_forbidden
    from renderer100 import GLOSSES
    from schema100 import Arm, Pack100, Row100
except (ImportError, OSError):
    if __name__ != "__main__":
        raise
    print("Cannot initialize experiment runner; check the training interpreter and required dependencies.", file=sys.stderr)
    raise SystemExit(1) from None


class FreezeReceipt(FrozenModel):
    """Must be authored/approved separately; this task does not create one."""

    pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    criteria_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_route_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_reference: str = Field(min_length=1)


def payload(row: Row100) -> tuple[Evidence, ...]:
    text = row.messages[1].content
    raw = text.split(EVIDENCE_START + "\n", 1)[1].split("\n" + EVIDENCE_END, 1)[0]
    return TypeAdapter(tuple[Evidence, ...]).validate_json(raw)


def validate_pack(pack: Pack100) -> None:
    """Reject pairing drift, unexpected A changes and stale message identities."""
    if len(pack.rows) != 3960 or len({r.id for r in pack.rows}) != 3960:
        raise ValueError("Requires 1320 questions and exactly three paired arms")
    groups: dict[str, dict[Arm, Row100]] = defaultdict(dict)
    for row in pack.rows:
        groups[row.question_id][row.arm] = row
        if row.id != f"{row.question_id}:{row.arm}" or tuple(m.role for m in row.messages) != ("system", "user"):
            raise ValueError("Invalid row identity/roles")
        digest = sha256("\n".join(m.model_dump_json() for m in row.messages).encode()).hexdigest()
        if digest != row.messages_sha256:
            raise ValueError("Message hash mismatch")
        if row.evidence_ids != tuple(e.chunk_id for e in payload(row)):
            raise ValueError("Rendered-to-source ID mapping mismatch")
    if len(groups) != 1320 or any(set(rows) != {"B", "A", "C"} for rows in groups.values()):
        raise ValueError("Missing question or arm")
    if len({rows["B"].persona_id for rows in groups.values()}) != 1320:
        raise ValueError("Requires unique families/personas")
    for group in groups.values():
        if set(group) != {"B", "A", "C"}:
            raise ValueError("Missing arm")
        b, a, c = (group[arm] for arm in ("B", "A", "C"))
        if len({r.persona_id for r in group.values()}) != 1 or a.evidence_ids != b.evidence_ids:
            raise ValueError("Pairing mismatch")
        if not (a.messages[0] == b.messages[0] == c.messages[0]):
            raise ValueError("Changed system prompt")
        original, changed = payload(b), payload(a)
        expected = []
        for e in original:
            text = e.content
            if e.speaker_role in GLOSSES:
                # Only validate an already constructed payload here. Actual O3
                # construction uses trusted chunks, never parsed source strings.
                text = "\n".join(line.partition(" | ")[0] + " | " +
                                 line.partition(" | ")[2].partition(" | ")[0] + " | " +
                                 GLOSSES[e.speaker_role] +
                                 line.partition(" | ")[2].partition(" | ")[2]
                                 for line in text.splitlines())
            expected.append(e.model_copy(update={"content": text}))
        if changed != tuple(expected):
            raise ValueError("A is not exclusively the O3 gloss")
        for r in (a, c):
            if r.messages[1].content.split(EVIDENCE_START, 1)[0] != b.messages[1].content.split(EVIDENCE_START, 1)[0] or (
                r.messages[1].content.split(EVIDENCE_END, 1)[1] != b.messages[1].content.split(EVIDENCE_END, 1)[1]
            ):
                raise ValueError("Arm changed the question or r1 instruction")
        for e in payload(c):
            if e.speaker_role != "persona":
                raise ValueError("C contains a non-persona utterance")


def main(pack_path: Path, model_path: Path, receipt_path: Path, output: Path) -> None:
    """One sequential batch-one run, no retries, submissions, networking or judges."""
    if os.environ.get("STAGE100_EXECUTE_DEV") != "YES":
        raise PermissionError("Separate DEV execution authorization is required")
    expected_node = os.environ.get("STAGE100_EXPECTED_NODE", "")
    node_label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    if len(expected_node) > 253 or re.fullmatch(rf"{node_label}(?:[.]{node_label})*", expected_node) is None:
        raise PermissionError("Expected one explicit Slurm hostname")
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURMD_NODENAME") != expected_node:
        raise PermissionError("Expected hostname must match the allocated Slurm node")
    reject_forbidden(pack_path, model_path, receipt_path, output)
    receipt = FreezeReceipt.model_validate_json(receipt_path.read_text())
    criteria = HERE / "CRITERIA.md"
    if "DRAFT" in criteria.read_text().splitlines()[0] or "NOT FROZEN" in criteria.read_text().splitlines()[0]:
        raise PermissionError("Criteria remain unfrozen; no generation allowed")
    for path, digest in ((pack_path, receipt.pack_sha256), (criteria, receipt.criteria_sha256),
                         (HERE / "ANNOTATION_RUBRIC.md", receipt.rubric_sha256)):
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Freeze receipt mismatch")
    pack = Pack100.model_validate_json(pack_path.read_text())
    validate_pack(pack)
    for relative, digest in pack.code_sha256.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Prepared code/profile changed")
    # Source paths are preparation provenance and may be on a different CPU host.
    # The approved pack digest binds their hashes and the exact prepared messages;
    # as in #95, execution checks staged code/profile and tokenizer, not CPU paths.
    if output.exists():
        raise FileExistsError(output)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    sys.path.append(str(ROOT / "experiments/model77"))
    from audit import serialize, token_ids
    from decision import model_default_options
    from smoke import load_profile, termination, think_markers
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    profile = load_profile("Qwen3-8B")
    config = json.loads((model_path / "config.json").read_text())
    metadata = model_path / ".cache/huggingface/download/config.json.metadata"
    if config["model_type"] != profile.model_type or (
        not metadata.exists() or metadata.read_text().splitlines()[0] != profile.revision
    ):
        raise ValueError("Model type/revision differs from pinned #77 profile")
    if sha256((model_path / "tokenizer.json").read_bytes()).hexdigest() != pack.tokenizer_sha256 or (
        sha256((model_path / "tokenizer_config.json").read_bytes()).hexdigest() != pack.tokenizer_config_sha256
    ):
        raise ValueError("Tokenizer differs from preparation")
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Requires one visible BF16 GPU")
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    rendered = [serialize(tokenizer, [m.model_dump() for m in r.messages], profile) for r in pack.rows]
    if any(len(token_ids(tokenizer, text)) + 160 > min(4096, config["max_position_embeddings"]) for text in rendered):
        raise ValueError("Prompt/output cap exceeded; no truncation")
    torch.cuda.set_device(0)
    model = AutoModelForCausalLM.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
        dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="eager",
    ).eval()
    if any(p.device.type != "cuda" or p.dtype != torch.bfloat16 for p in model.parameters()):
        raise RuntimeError("All weights must be BF16 on CUDA")
    eos = model.generation_config.eos_token_id
    eos_ids = [eos] if isinstance(eos, int) else eos
    if not eos_ids:
        raise ValueError("Missing native EOS")
    decoding = GenerationConfig(
        max_new_tokens=160, do_sample=False, num_beams=1, num_return_sequences=1,
        repetition_penalty=1.0, min_length=0, min_new_tokens=0, eos_token_id=eos_ids,
        pad_token_id=tokenizer.pad_token_id or eos_ids[0], max_time=30.0,
    )
    options = model_default_options(signature(model.generate).parameters)
    with output.open("x", encoding="utf-8") as handle:
        for row, text in zip(pack.rows, rendered, strict=True):
            start = time.perf_counter()
            inputs = tokenizer(text, add_special_tokens=False, truncation=False, return_tensors="pt").to("cuda:0")
            inputs.pop("token_type_ids", None)
            count = inputs["input_ids"].shape[1]
            torch.cuda.synchronize()
            generation_start = time.perf_counter()
            with torch.inference_mode():
                generated = model.generate(**inputs, generation_config=decoding, **options)
            torch.cuda.synchronize()
            seconds = time.perf_counter() - generation_start
            ids = generated[0, count:].tolist()
            raw, answer = (tokenizer.decode(ids, skip_special_tokens=skip) for skip in (False, True))
            wall = time.perf_counter() - start
            valid = bool(answer.strip() and ids) and not think_markers(raw) and wall <= 30
            handle.write(json.dumps({
                "scope": "DEV_ONLY", "id": row.id, "question_id": row.question_id, "arm": row.arm,
                "model": profile.model_id, "revision": profile.revision,
                "messages_sha256": row.messages_sha256, "pack_sha256": receipt.pack_sha256,
                "criteria_sha256": receipt.criteria_sha256, "job_id": os.environ["SLURM_JOB_ID"],
                "text": answer, "raw_text": raw, "generated_ids": ids,
                "generated_ids_sha256": sha256(json.dumps(ids).encode()).hexdigest(),
                "input_tokens": count, "output_tokens": len(ids), "wall_seconds": wall,
                "generation_seconds": seconds, "termination": termination(ids, eos_ids),
                "status": "success" if valid else "failed", "batch": 1, "dtype": "bfloat16",
            }, ensure_ascii=False) + "\n")
            handle.flush()
            if not valid:
                raise RuntimeError(f"Failed DEV row {row.id}; no regeneration")
    print("DEV_COMPLETE rows=3960 judges_run=0")


def cli(pack_path: Path, model_path: Path, receipt_path: Path, output: Path) -> None:
    """One sequential batch-one run, no retries, submissions, networking or judges."""
    # Typer's exception rendering can disclose both arguments and frame locals.
    try:
        main(pack_path, model_path, receipt_path, output)
    except FileExistsError:
        message = "Output already exists"
    except FileNotFoundError as error:
        message = (
            "Freeze receipt file not found"
            if error.filename == os.fspath(receipt_path)
            else "Required experiment artifact not found"
        )
    except PermissionError as error:
        reasons = (
            "Separate DEV execution authorization is required",
            "Expected one explicit Slurm hostname",
            "Expected hostname must match the allocated Slurm node",
            "Criteria remain unfrozen; no generation allowed",
        )
        message = next((reason for reason in reasons if error.args == (reason,)), "Experiment access denied")
    except OSError:
        message = "Could not access an experiment artifact"
    except (ValueError, KeyError, IndexError) as error:
        # Validation exceptions can carry input fragments and artifact paths.
        reasons = (
            "Freeze receipt mismatch",
            "Prepared code/profile changed",
            "Model type/revision differs from pinned #77 profile",
            "Tokenizer differs from preparation",
        )
        message = next((reason for reason in reasons if error.args == (reason,)), "Invalid experiment data or configuration")
    except RuntimeError:
        # Model/runtime errors can include resolved model and artifact paths.
        message = "Experiment runtime failed"
    else:
        return
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    stdout, stderr = sys.stdout, sys.stderr
    startup_stdout, startup_stderr = StringIO(), StringIO()
    command_started = False

    @wraps(cli)
    def invoke_cli(pack_path: Path, model_path: Path, receipt_path: Path, output: Path) -> None:
        global command_started
        command_started = True
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cli(pack_path, model_path, receipt_path, output)

    try:
        # Publish parser/help output only after successful rendering. Runtime
        # output still streams when the callback runs.
        with redirect_stdout(startup_stdout), redirect_stderr(startup_stderr):
            app = typer.Typer(add_completion=False)
            app.command()(invoke_cli)
            status = app(standalone_mode=False)
        # Do not queue bytes in the original text wrappers: an unsuccessful
        # buffered flush can be retried by Python during interpreter shutdown.
        def publish(fd: int, text: str, stream: TextIO) -> None:
            encoding = getattr(stream, "encoding", None) or "utf-8"
            errors = getattr(stream, "errors", None) or "strict"
            remaining = memoryview(text.encode(encoding, errors))
            while remaining:
                try:
                    written = os.write(fd, remaining)
                except InterruptedError:
                    continue
                if written <= 0 or written > len(remaining):
                    raise OSError("CLI output write made invalid progress")
                remaining = remaining[written:]

        if status in (None, 0):
            publish(1, startup_stdout.getvalue(), stdout)
            publish(2, startup_stderr.getvalue(), stderr)
    except click.UsageError:
        print("Invalid arguments. Run with --help for usage.", file=stderr)
        raise SystemExit(2) from None
    except click.Abort:
        print("Aborted!", file=stderr)
        raise SystemExit(1) from None
    except Exception:
        # Lazy imports, rendering, stream writes and unexpected runtime faults
        # can contain local paths. Never render their values at the executable
        # boundary; direct library calls retain the original exceptions.
        print("Experiment runner failed; check the runtime dependencies and configured inputs.", file=stderr)
        raise SystemExit(1) from None
    finally:
        if not command_started:
            # Parser/help never used the original buffered stdout. Leave an
            # inert stream for Python to flush, not the publication destination.
            sys.stdout = startup_stdout
    raise SystemExit(status)
