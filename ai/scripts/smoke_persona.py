"""Synthetic GPU endpoint smoke; no submission, downloads, retries, or answer logs.

Run INSIDE a separately authorized Slurm allocation. Starts one loopback server
with one real model. Slot checks gate the first real model forward via a hook;
they are instrumented GPU checks, not natural-overlap performance measurements.
"""
from http.client import HTTPConnection
import json
import math
from pathlib import Path
import sys
import threading
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import baseline
from inference import Inference
import persona_contract
import server
from smoke_baseline import payload, send


def persona_payload(*, evidence: bool = True, budget: int = 160):
    """Only synthetic content; no records or evaluation corpus are read."""
    chunk = {"chunk_id": str(uuid4()), "record_id": str(uuid4()),
             "text_version_id": str(uuid4()), "speaker_role": "persona",
             "content": "2024-07-12 | 온유 (speaker_role=persona) | 여름에 부산에 다녀왔다."}
    return {
        **payload("2024년 여름에 어디에 다녀왔지?", budget=budget),
        "persona_id": str(uuid4()),
        "persona": {"display_name": "온유", "provider_address": None},
        "source_revision": 1,
        "style_profile": {
            "id": str(uuid4()), "version": 1, "source_revision": 1,
            "source_snapshot_id": str(uuid4()),
            "content": {
                "schema_version": 1,
                "speech_style": {"tone": ["해요체"], "address_terms": [],
                                 "sentence_endings": [], "frequent_expressions": []},
                "conversation_habits": [], "limitations": []}},
        "evidence": [chunk] if evidence else [],
    }


class ObservedService(server.Service):
    """Expose exact admission/completion signals, without changing slot behavior."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted = threading.Event()
        self.job = None

    def accept(self, request, prepared):
        status, job = super().accept(request, prepared)
        if job is not None:
            self.job = job
            self.accepted.set()
        return status, job


def slot_check(httpd: server.Server, service: ObservedService, timeout: float,
               *, disconnect: bool) -> bool:
    """Probe both routes while the real first CUDA forward has not returned."""
    entered, release = threading.Event(), threading.Event()
    service.accepted.clear()
    service.job = None
    hook_ok = []

    def forward_hook(module, inputs, output):
        if not entered.is_set():
            entered.set()
            hook_ok.append(release.wait(timeout))
            if not hook_ok[-1]:
                raise RuntimeError

    handle = service.engine.model.register_forward_hook(forward_hook)
    first = payload("Say hello briefly.", budget=8)
    service.deadline_seconds = None if disconnect else 0.01
    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=timeout)
    passed = False
    try:
        connection.request("POST", "/v1/baseline/generate", body=json.dumps(first).encode(),
                           headers={"Content-Type": "application/json"})
        if not service.accepted.wait(timeout) or not entered.wait(timeout):
            return False
        if disconnect:
            connection.close()  # The GPU worker is already inside model.forward.
        else:
            response = connection.getresponse()
            body = json.loads(response.read())
            if (response.status != 504
                    or body != server.error_body(504, first["request_id"])):
                return False
        duplicate = persona_payload()
        duplicate["request_id"] = first["request_id"]
        other = persona_payload()
        status1, body1 = send(httpd.server_port, timeout, duplicate,
                              path="/v1/persona/generate")
        status2, body2 = send(httpd.server_port, timeout, other,
                              path="/v1/persona/generate")
        passed = (
            status1 == 409 and body1 == server.error_body(409, first["request_id"])
            and status2 == 429 and body2 == server.error_body(429, other["request_id"])
            and not service.job.done.is_set())
    finally:
        release.set()
        handle.remove()
        connection.close()
        completed = service.job is not None and service.job.done.wait(timeout)
        service.deadline_seconds = None
    return passed and completed and hook_ok == [True] and service.job.status == 200


def run(httpd: server.Server, service: ObservedService, timeout: float) -> int:
    failures = 0

    def report(name, passed, status=None):
        nonlocal failures
        failures += not passed
        print(json.dumps({"case": name, "result": "PASS" if passed else "FAIL",
                          "status": status}), flush=True)

    def check(name, data, expected=200, *, citations_required=False):
        parsed = persona_contract.parse_request(data)
        status, body = send(httpd.server_port, timeout, data, path="/v1/persona/generate")
        valid = status == expected and body is not None
        if valid and status == 200:
            context = body.get("context", {})
            valid = (
                set(body) == {"request_id", "message", "finish_reason", "ai_generated",
                              "context", "applied_profile", "evidence_used", "model_name"}
                and body["request_id"] == parsed.request_id
                and isinstance(body["message"], str) and bool(body["message"].strip())
                and len(body["message"]) <= 16000
                and body["finish_reason"] in ("stop", "length")
                and body["ai_generated"] is True
                and body["model_name"] == service.persona_generator.model_name
                and body["applied_profile"] == {
                    "id": parsed.style_profile.id, "version": parsed.style_profile.version,
                    "source_revision": parsed.style_profile.source_revision}
                and set(context) == {"used_history_turns", "dropped_history_turns"}
                and all(type(n) is int and n >= 0 for n in context.values())
                and sum(context.values()) == len(parsed.history)
                and isinstance(body["evidence_used"], list)
                and len(set(body["evidence_used"])) == len(body["evidence_used"])
                and set(body["evidence_used"]) <= {e.chunk_id for e in parsed.evidence}
                and (not citations_required or bool(body["evidence_used"])))
        elif valid:
            valid = body == server.error_body(expected, parsed.request_id)
        report(name, valid, status)

    status, body = send(httpd.server_port, timeout, method="GET", path="/healthz")
    report("health", status == 200 and body == {"ready": True}, status)
    status, body = send(httpd.server_port, timeout, method="GET", path="/readyz")
    report("ready", status == 200 and body == {"ready": True}, status)
    check("grounded-citation", persona_payload(), citations_required=True)
    check("no-evidence-personal", persona_payload(evidence=False))
    daily = persona_payload(evidence=False)
    daily["message"] = "안녕!"
    check("no-evidence-daily", daily)
    history = persona_payload()
    history["history"] = [{"user": "안녕.", "assistant": "안녕하세요."}]
    check("history", history)
    # One token cannot complete this JSON envelope: report the safe failure, no retry.
    check("one-token-envelope", persona_payload(budget=1), 500)
    status, body = send(httpd.server_port, timeout, payload())
    report("explicit-baseline", status == 200 and body is not None
           and bool(body.get("message", "").strip()) and "applied_profile" not in body, status)
    for disconnect in (True, False):
        report("disconnect-slot" if disconnect else "deadline-slot",
               slot_check(httpd, service, timeout, disconnect=disconnect))
    print(json.dumps({"failures": failures, "suite": "persona-gpu-smoke"}), flush=True)
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = baseline.SafeArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--config", type=Path, default=baseline.DEFAULT_CONFIG)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError
    settings = baseline.read_settings(args.config, "smoke", args.model_path)
    if settings["model_id"] != "Qwen/Qwen3-8B":
        raise ValueError  # This prepared smoke is specifically the Qwen3-8B baseline.
    service = ObservedService()
    with server.Server(0, service) as httpd:
        service.start_loading(lambda: Inference(settings), persona_model_name=settings["model_id"])
        service.loader.join(args.timeout)
        if service.loader.is_alive() or not service.ready.is_set():
            print('{"case":"model-load","result":"FAIL"}')
            return 1
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            return run(httpd, service, args.timeout)
        finally:
            httpd.shutdown()
            thread.join(args.timeout)
            if thread.is_alive():
                raise RuntimeError


if __name__ == "__main__":
    raise SystemExit(main())
