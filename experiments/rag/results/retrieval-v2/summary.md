# #59 retrieval v2 offline results

## 1. 기준선 재계산 및 정책 고정

사전 수용 기준: 기존과 동일하게 고정(변경 없음).

코퍼스 및 모델 리비전: 보정된 코퍼스 적용 및 KURE 리비전 `8b418a58414668e75532ed045c22d9ca018ae2b2` 고정.

B_old 재계산: CPU 환경에서 재계산 수행.

DEV 임계값: `0.5458711981773378` (기존 선정 정책 동일 적용: DEV unanswerable F1 최대화 $\rightarrow$ false empty 최소화 $\rightarrow$ 더 작은 임계값 순).

## 2. 컨텍스트 파라미터 탐색 (DEV-only)

탐색 공간: 총 27개 후보 조합 (`k = 3, 4, 5`, `window = 1, 2, 3`, `budget = 1500, 1750, 2000`), DEV 세트에서만 탐색 후 고정된 설정으로 TEST 1회 평가.

선정 기준: 적격 조건(`DEV median <= 1500`) 내에서 factual complete 최대화 $\rightarrow$ slots 최대화 $\rightarrow$ median/max 토큰 최소화 $\rightarrow$ 더 작은 파라미터 설정 순.

최종 선정: `k=3, window=±3, hardcap=2000` (DEV 29/30, slots 92/94, median 1432.5, max 1971).

| 후보 설정 (`k / window / budget`) | DEV Factual Complete | DEV Slots | DEV Median Tokens | DEV Max Tokens | 비고 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `k=3, window=±3, hardcap=2000` | 29/30 | 92/94 | 1432.5 | 1971 | 최종 선정 |
| `k=3, window=±2, budget=2000` | 27/30 | - | 1346.5 | - | 비교 후보 |
| `k=4, window=±2, budget=2000` | 29/30 | - | 1483.5 | - | 비교 후보 |
| `k=5, window=±2, budget=2000` | 29/30 | - | 1491.0 | - | 비교 후보 |

## 3. 오프라인 검색 평가 (B_old vs B_v2)

| 지표 | 세부 내용 | B_old | B_v2 | 수용 기준 | 판정 |
| :--- | :--- | :---: | :---: | :---: | :---: |
| R1 | 전체 Factual Complete (60문항) | 42/60 | 57/60 | 최소 54/60 | PASS |
| R2 | TEST Factual Complete (30문항) | 19/30 | 28/30 | 최소 27/30 | PASS |
| R3 | TEST Unanswerable Empty (5문항) | 2/5 | 2/5 | 악화 없음 | PASS |
| R4 | Ordinary Empty (전체 10문항) | 5/10 | 5/10 | 악화 없음 | PASS |
| R5 | 전체 80문항 토큰 중앙값 (Qwen tokens) | 427.5 | 1449.0 | 최대 1500 | PASS |


세부 분포: Ordinary empty는 B_old와 B_v2 모두 `DEV 4/5, TEST 1/5`로 동일하며, Factual empty는 양쪽 모두 `0/60`.

토큰 집계 방식: UUID 및 메타데이터를 포함한 전체 JSON evidence 문자열 기준, `Qwen/Qwen3-8B` 토크나이저(리비전 `b968826d9c46dd6066d109eabc6255188de91218`, 토크나이저 파일만 다운로드)로 계산. 최대(max) 토큰은 `B_old 950`, `B_v2 1997`.

## 4. 프롬프트 포맷 및 컨텍스트 병합 규칙

r1 프롬프트 불변성: instructions, system, persona는 변경하지 않고 유지. Evidence JSON의 `content`와 포함 청크만 변경(동일한 5개 필드 유지).

라인 프리픽스: 각 `content` 라인 앞에 `date | speaker (speaker_role=role) |` 프리픽스 부여.

컨텍스트 구성 및 예산 절삭:
  * 정규 청크는 `record-date` 및 `turn` 순서로 중복 제거.
  * 채팅 윈도우는 청크 경계(각 청크당 동일 화자 최대 2턴)에서 병합.
  * 짧은 일기는 전문 포함.
  * 예산 초과 시 점수가 가장 낮은 소스 레코드부터 절삭.

## 5. 무결성 검증 및 한계

원본 보존 및 감사: 과거 코퍼스 git HEAD 바이트 스냅샷 및 소스 스냅샷 보존. 기존 `kure` / `e5` / `generation` (`r1`, `r2`, `r3`) 파일 미수정. 프롬프트 바이트 동일성 검사 3건 모두 통과 및 과거 `kure`/`e5` 감사 통과.

드라이런 및 테스트: `r1` 드라이런(`B_old`, `B_v2`, `C` 각 80건, 총 240건) 완료. 전체 폴더 `pytest` 108개 통과 (3.16s 소요, 알려진 Typer/Click 경고 2건).

미수행 항목 및 보류 상태: Git 커밋, SSH, GPU 생성, Judge 평가 미수행. `G1–G5` 생성/판정 지표가 대기 상태이므로 최종 채택 주장은 보류함.
