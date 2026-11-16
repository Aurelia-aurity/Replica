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

## TTFT 계측 드라이버
`measure_gpu.py --double`은 CPU tensor/model doubles와 실제 준비·생성·검증 코드를 연결합니다. 모든 wave에서 worker 종료를 확인한 뒤 request ID와 attempt ID로 서버 로그를 결합합니다. 클라이언트 JSONL의 `server_spans`는 구간별 호출 횟수와 `duration_ns` 합을 담습니다. 같은 `template_tokenize`의 반복 시도만 합산하며 서로 다른 중첩 구간은 더하지 않습니다. 첫 토큰 필드의 기준은 `ttft_source="server_monotonic"`이고 `measurement_kind="double"`로 합성 측정을 표시합니다. 거부 요청처럼 worker 종료 기록이 없으면 TTFT는 null이며 `ttft_reason="no_worker_terminal"`입니다.

`analyze_timing.py <결과 디렉터리>`는 phase·arm·근거 수·제공 동시성·HTTP 상태별로 새 지표의 n·p50·p95·최댓값과 TTFT 누락 수를 계산합니다. 표본이 없으면 null이며 percentile은 nearest-rank 방식입니다. `first_unforced_token_ns`는 요청 시작 기준 ns, 나머지 첫 토큰 시간은 ms, 구간 시간은 ns입니다. 기존 `measure_local.py --rounds 1`은 28개 loopback 요청을 실행하며 서버 로그를 결합하지 않아 클라이언트의 TTFT는 계속 미관측으로 표시합니다.

이 확장은 아직 동결하지 않았습니다. job 55389의 v1 기준·manifest·원시 결과는 그대로 보존하며 새 필드로 과거 값을 추정하지 않습니다. 실모델 실행 전에는 새 protocol ID와 source manifest를 검토·동결하고 별도 실행 승인을 받아야 합니다. doubles 시간으로 GPU 성능이나 STT-to-playback 완료를 주장하지 않습니다.
