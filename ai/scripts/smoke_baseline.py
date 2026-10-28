"""Synthetic localhost smoke; never prints prompt/answer text or raw exceptions."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPException
import json
import math
import threading
import uuid


def payload(message="Give one short study tip.", history=None, budget=160):
    return {"request_id": str(uuid.uuid4()), "message": message,
            "history": [] if history is None else history, "max_new_tokens": budget}


def send(port, timeout, data=None, sent=None, method="POST", path="/v1/baseline/generate",
         raw_limit=False):
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        if raw_limit:
            body = b""
            headers["Content-Length"] = "262145"
        connection.request(method, path, body=body, headers=headers)
        if sent is not None:
            sent.set()
        response = connection.getresponse()
        result = json.loads(response.read())
        return (response.status, result) if isinstance(result, dict) else (None, None)
    except (OSError, HTTPException, ValueError):
        return None, None  # Transport failure is UNKNOWN, never an invented AI 504.
    finally:
        if sent is not None:
            sent.set()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, help="Client wait only; unset by default, not AI cancellation")
    parser.add_argument("--history-chars", type=int, default=2500)
    parser.add_argument("--context-chars", type=int, default=16000)
    args = parser.parse_args(argv)
    if (not 1 <= args.port <= 65535 or not 1 <= args.history_chars <= 6000
            or not 1 <= args.context_chars <= 16000
            or args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0)):
        parser.error("invalid port, text size, or client timeout")
    failures = 0
    inconclusive = 0

    def check(name, data, expected_status, predicate=lambda body: True, raw_limit=False):
        nonlocal failures, inconclusive
        status, body = send(args.port, args.timeout, data, raw_limit=raw_limit)
        correlated = body is not None and body.get("request_id") == (
            None if raw_limit else data["request_id"])
        context = (body or {}).get("context")
        success = (
            correlated and isinstance(body.get("message"), str) and bool(body["message"].strip())
            and body.get("ai_generated") is True and body.get("finish_reason") in ("stop", "length")
            and isinstance(context, dict)
            and set(context) == {"used_history_turns", "dropped_history_turns"}
            and all(type(value) is int and value >= 0 for value in context.values())
            and sum(context.values()) == len(data["history"]))
        try:
            passed = (status == expected_status and correlated
                      and (status != 200 or success) and predicate(body))
        except (KeyError, TypeError):
            passed = False
        if passed:
            outcome = "PASS"
        elif status == 200 and expected_status in (200, 422) and success:
            outcome = "INCONCLUSIVE"  # Actual tokenizer/model did not induce the branch.
            inconclusive += 1
        else:
            outcome = "FAIL"
            failures += 1
        print(json.dumps({"case": name, "result": outcome, "request_id": data["request_id"],
                          "status": status, "finish_reason": (body or {}).get("finish_reason"),
                          "context": (body or {}).get("context")}))

    status, body = send(args.port, args.timeout, method="GET", path="/readyz")
    if status != 200 or body != {"ready": True}:
        print("NOT_READY_OR_UNKNOWN: inspect the job and check /readyz before running smoke.")
        return 1
    check("normal", payload(), 200)
    check("history-kept", payload(history=[{"user": "I have ten minutes.", "assistant": "Pick one task."}]),
          200, lambda b: b["context"] == {"used_history_turns": 1, "dropped_history_turns": 0})
    text = ("alpha beta gamma delta 0123456789 " * 600)[:args.history_chars]
    check("history-trimmed", payload(history=[{"user": text, "assistant": text}] * 20),
          200, lambda b: b["context"]["dropped_history_turns"] > 0 and
          sum(b["context"].values()) == 20)
    check("length", payload("List integers from 1 to 1000, separated by commas. Start with 1.", budget=8),
          200, lambda b: b["finish_reason"] == "length")
    check("parsed-oversized", payload("x" * 16001), 413,
          lambda b: b["error"]["code"] == "PAYLOAD_TOO_LARGE")
    check("raw-oversized", payload(), 413,
          lambda b: b["error"]["code"] == "PAYLOAD_TOO_LARGE", raw_limit=True)
    long_text = ("alpha beta gamma delta 0123456789 " * 600)[:args.context_chars]
    check("context-exceeded", payload(long_text, budget=1024), 422,
          lambda b: b["error"]["code"] == "CONTEXT_LIMIT_EXCEEDED")

    # The wire has no acceptance acknowledgment. Body-sent is not proof of GPU
    # acceptance; report missing overlap as inconclusive rather than claiming a pass.
    first = payload("List integers from 1 to 10000 with explanations, without stopping.", budget=1024)
    other = {**first, "request_id": str(uuid.uuid4())}
    sent = threading.Event()
    with ThreadPoolExecutor(max_workers=3) as pool:
        running = pool.submit(send, args.port, args.timeout, first, sent)
        sent.wait()
        duplicate = pool.submit(send, args.port, args.timeout, first)
        busy = pool.submit(send, args.port, args.timeout, other)
        outcomes = [running.result(), duplicate.result(), busy.result()]
    for name, req, result, expected in (
            ("duplicate", first, outcomes[1], 409), ("busy", other, outcomes[2], 429)):
        status, body = result
        correlated = body is not None and body.get("request_id") == req["request_id"]
        expected_code = "REQUEST_IN_PROGRESS" if expected == 409 else "AI_BUSY"
        if status == expected and correlated and body.get("error", {}).get("code") == expected_code:
            outcome = "PASS"
        elif correlated and status in (200, 409, 429):
            outcome = "INCONCLUSIVE"
            inconclusive += 1
        else:
            outcome = "FAIL"
            failures += 1
        print(json.dumps({"case": name, "result": outcome, "request_id": req["request_id"], "status": status}))
    if outcomes[0][0] != 200:
        # A different wire request can win the slot; sending first does not prove
        # acceptance first. This does not establish a service failure.
        if (outcomes[0][0] in (409, 429) and outcomes[0][1] is not None
                and outcomes[0][1].get("request_id") == first["request_id"]):
            print("FIRST_CONCURRENT_REQUEST_OVERLAP_INCONCLUSIVE")
            inconclusive += 1
        else:
            print("FIRST_CONCURRENT_REQUEST_NOT_SUCCESSFUL")
            failures += 1
    print(json.dumps({"failures": failures, "inconclusive": inconclusive}))
    return 1 if failures else (2 if inconclusive else 0)


if __name__ == "__main__":
    raise SystemExit(main())
