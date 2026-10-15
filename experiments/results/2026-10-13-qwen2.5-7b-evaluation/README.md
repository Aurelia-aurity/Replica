# Qwen2.5-7B-Instruct 평가 결과 (#36)

학교 GPU 실행일: 2026-10-13. 평가 반영일: 2026-10-14.

## 결과

문항별 점수와 해석은 [전체 평가](전체평가.md)에 기록했다.

| 평가 | 결과 | 범위 |
| --- | ---: | --- |
| 사용자 전체 점수 | 3.80/5 | 20답변, 단일 평가자, 비블라인드 |
| 기록 QA 전체 정답률 | 50.00% | 20문항 × 3회, 문자열 Exact Match |
| JSON 형식 준수율 | 100.00% | 기록 QA |
| 지시 전체 조건 충족률 | 75.00% | 20문항 × 3회 |
| KoBEST 5과제 동일 가중 평균 | 67.54% | 전체 4,561문항, 프로젝트 집계 |

사용자 점수는 [답안과 평가 사유](사람평가-20개-답안.md), [점수 JSON](human-ratings-002.json)에 연결된다. AI가 작성한 [새 20개 의미 판정](세부의미판정-20개.md)과 [기존 5개 소급 판정](기존33-근거5개-소급평가.md)은 사용자 결정을 반영했으며 독립된 사람의 재평가로 보지 않는다. 기존 #33의 네 영역별 점수와 원본 결과는 보존했다. 서로 다른 평가 축을 하나의 점수로 합치지 않는다.

## 파일 구성과 출처

- `downloaded-results-001/`: 합성 문항의 실제 생성 로그·입력·식별 정보와 기계 채점 보고서. 원본과 바이트 단위로 같다. 비어 있는 사람 판정 양식도 원본 그대로 보존했다.
- `semantic-reviewed-001/`, `human-semantic-scoring-001/`: 최종 새 20개 의미 판정과 집계.
- `retrospective-semantic-reviewed-001/`, `retrospective-semantic-scoring-001/`: 기존 G01–G05의 최종 소급 판정과 집계. `source/`의 원래 실행 로그에 연결되며 원래 점수를 대체하지 않는다.
- `collected-002/report.json`: 새 20개, 두 기계 과제, KoBEST의 통합 결과. 이 파일의 `retrospective_baseline.state=not_supplied`는 별도로 제공한 소급 판정이 미완료라는 뜻이 아니다.
- `kobest-sample-metrics.json`: 4,561문항의 번호·선택지 likelihood·정오 값만 원본에서 추출했다. KoBEST 원문·렌더링된 prompt·개인 경로·캐시·모델 가중치는 제외했다. 원본 harness 결과를 대신하는 파일이 아니다.
- `provenance.json`, `published-files.json`: 실행·판정 주체, 계획과 실제의 차이, 보존한 원본 hash와 공개 파일 hash.

입력과 코드 버전은 각 run의 `metadata.json`, `release.json`과 `identity.json`에 기록돼 있다. 모델 파일 hash는 같지만 upstream revision과 추출 번들의 실행 commit은 미검증이다. 상세 실패·미측정 항목과 평가 한계는 [전체 평가](전체평가.md)에 있다. 합성 응답은 공개 전 개인정보·민감 내용과 개인 경로를 확인했다.

## 재채점과 후속 비교

저장소 루트에서 표준 라이브러리만으로 확인한다. `-B`는 검사 중 Python 캐시 파일 생성을 막는다.

```bash
python3 -B experiments/results/2026-10-13-qwen2.5-7b-evaluation/verify.py
```

이 명령은 공개 파일 hash, 사용자 점수 합계, 세 생성 과제의 원본 재채점, G01–G05 소급 집계, KoBEST 문항별 수·likelihood·정오 값 집계를 확인한다. 의미 판정의 타당성이나 독립성을 자동 검증하는 명령은 아니다. KoBEST 원본 전체 파일은 Git 밖에 보관하며 원본 hash 검증을 포함한 `collect.py` 재실행에는 그 파일이 필요하다. 투영 파일을 원본 경로에 덮어쓰지 않는다.

새 실행과 최종 의미 판정 파일을 준비하는 절차는 [평가 README](../../evaluation/README.md)에 있다. #37에서 동일 입력·생성 설정·판정 규칙을 유지해 모델별로 별도 경로에 실행한다. 모델·tokenizer·template·dtype·패키지·GPU·코드 hash가 달라지면 그 차이를 기록한다. 동일 출력 재채점의 재현성과 GPU에서 새로 생성한 답변의 동일성은 구분한다. 블라인드 평가를 새로 수행하기 전에는 이번 점수를 블라인드 기준값으로 표기하지 않는다.

PR 검토와 이슈 종료는 평가 데이터 완료와 별도다. 이번 결과로 서비스 적용이나 특정 모델 선정을 확정하지 않는다.
