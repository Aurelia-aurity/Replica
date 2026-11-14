"""Local synthetic #44 HTTP harness, NOT production backend integration.

Run from the repository root: python3 -B ai/scripts/local_persona_transport.py
Uses the real HTTP handler, PersonaInference and Inference.generate with stdlib
tokenizer/model/CUDA doubles. No weights, GPU, credentials, remote access or
automatic retries. Printed records contain metadata only. This proves transport
and schema handling, never model quality, latency or a school connection.
"""
from contextlib import nullcontext
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from inference import Inference
from persona_adapter import PersonaInference
import server
from smoke_persona import persona_payload

MODEL_NAME = "local-synthetic-model"


class SyntheticTokenizer:
    """Code points stand in for model tokens; no trained tokenizer is loaded."""
    eos_token_id = 0

    def encode(self, text, **kwargs):
        return [ord(char) for char in text]

    def apply_chat_template(self, messages, **kwargs):
        return [ord(char) for message in messages for char in message["content"]]

    def decode(self, tokens, **kwargs):
        return "".join(chr(token) for token in tokens if token != self.eos_token_id)


def synthetic_engine():
    """Keep real decode orchestration, replace tensor/CUDA and model operations."""
    engine = Inference.__new__(Inference)
    engine.system_prompt = "Synthetic persona transport only."
    engine.context_limit = 100000
    engine.tokenizer = SyntheticTokenizer()
    engine.torch = Mock()
    engine.torch.inference_mode = nullcontext
    engine.generation_config = lambda **kwargs: SimpleNamespace(**kwargs)
    engine.model = Mock()
    engine.model.generation_config.eos_token_id = 0
    return engine


def validate_response(data, status, body):
    """Check #93 profile identity, citation subset, correlation and model label."""
    assert status == 200, f"unexpected HTTP status {status}"
    assert set(body) == {
        "request_id", "message", "finish_reason", "ai_generated", "context",
        "applied_profile", "evidence_used", "model_name"}
    assert body["request_id"] == data["request_id"], "response ID mismatch"
    assert body["applied_profile"] == {
        key: data["style_profile"][key] for key in ("id", "version", "source_revision")}
    assert body["model_name"] == MODEL_NAME, "model label mismatch"
    assert isinstance(body["message"], str) and body["message"].strip()
    assert body["ai_generated"] is True
    assert body["finish_reason"] in ("stop", "length")
    citations = body["evidence_used"]
    assert isinstance(citations, list) and all(isinstance(item, str) for item in citations)
    assert len(citations) == len(set(citations))
    assert set(citations) <= {item["chunk_id"] for item in data["evidence"]}
    assert body["context"] == {
        "used_history_turns": len(data["history"]), "dropped_history_turns": 0}


def main():
    engine = synthetic_engine()
    service = server.Service(engine, persona_generator=PersonaInference(engine, MODEL_NAME))
    with server.Server(0, service) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            for index, case in enumerate(("evidence", "no-evidence", "history"), start=1):
                data = persona_payload(evidence=case != "no-evidence")
                if case == "history":
                    data["history"] = [{"user": "Synthetic greeting.", "assistant": "Hello."}]
                raw_output = json.dumps({
                    "message": "Synthetic reply.", "evidence_used": [1] if data["evidence"] else []})
                tokens = engine.tokenizer.encode(raw_output) + [0]
                output = Mock()
                output.__getitem__ = Mock(return_value=SimpleNamespace(tolist=lambda: tokens))
                engine.model.generate.return_value = output
                connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                try:
                    connection.request("POST", "/v1/persona/generate", json.dumps(data),
                                       {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    validate_response(data, response.status, body)
                finally:
                    connection.close()
                assert engine.model.generate.call_count == index, "unexpected model replay"
                assert engine.torch.cuda.synchronize.call_count == index
                expected = [data["evidence"][0]["chunk_id"]] if data["evidence"] else []
                assert body["evidence_used"] == expected, "citation mapping mismatch"
                print(json.dumps({
                    "scope": "local-synthetic-only", "case": case,
                    "request_id": data["request_id"], "status": response.status,
                    "profile_checked": True, "evidence_subset_checked": True,
                    "model_name": body["model_name"], "model_calls": index}))
        finally:
            httpd.shutdown()
            thread.join(5)
            assert not thread.is_alive(), "local HTTP server failed to stop"
    print(json.dumps({"scope": "local-synthetic-only", "cases": 3, "failures": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
