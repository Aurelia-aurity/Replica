# Local warmup/timing protocol skeleton

This unfrozen skeleton starts its own loopback HTTP server with inference doubles.
It has no external-server option and does not load weights or access a GPU.
From the repository root:

```bash
/usr/local/bin/python3 -B experiments/warmup_timing/measure_local.py --rounds 1
```

Each round offers 1, 2 and 4 simultaneous requests for each synthetic evidence
count 0, 3, 6 and 10 (28 attempts). The cell order rotates each round. A barrier
releases each wave on separate connections; all responses finish before the next
wave. There are no retries, artificial delays or queue. With C=1, requests can
return 429 or run serially within the offered wave; do not assume C-1 rejections.
The doubles have no server deadline and successful responses follow worker
completion, so terminal HTTP responses suffice to drain these local waves.

Stdout is metadata-only JSONL: request UUID, fixture SHA-256, evidence count,
offered concurrency, wave, participant, client monotonic send/completion times
and HTTP status. `protocol_frozen=false` and `ttft_available=false` are explicit.
Server timing uses the separate `baseline.http` logger when enabled. No model
output, profile, prompt or evidence content is printed.

This exercises fixture parsing, startup warmup and the HTTP/concurrency harness.
It does not implement fresh-process A/B controls, a frozen manifest, real worker
drain subscriptions for timed-out requests, percentile analysis, school execution,
GPU latency or STT-to-playback timing. Those require a reviewed frozen protocol
and separate run authorization. Do not use double timings as performance evidence.
