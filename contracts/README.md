# API Contracts

앱↔백엔드↔AI 서버의 연동 약속을 관리합니다.

baseline 구현 전에 다음을 합의합니다.

- 텍스트 대화 요청·응답과 AI 생성 표시
- 음성 입력·출력의 파일 형식과 전달 방식
- 오류 코드·오류 응답·타임아웃 처리
- 스트리밍 여부와 스트리밍 이벤트 형식
- 합성 데이터로 작성한 요청·응답 예시

아직 확정된 API 명세는 없습니다.
OpenAPI 등 실제 명세를 도입할 때 관리 원본과 생성 절차를 하나로 정해 중복 편집을 피합니다.

## 초안

- [백엔드↔AI baseline 호출 계약](baseline/README.md): #15 요청·응답·오류 규격,
  대화 이력·처리 한도·타임아웃 책임과 합성 예제. 담당자 합의 전이며 API 구현은 포함하지 않습니다.
- 구조 규격은 baseline의 JSON Schema를 원본으로 관리합니다.
  검증: `python3 -m unittest discover -s contracts/tests -v`

## 백엔드↔AI 고도화 합의안

- [통합 호출 명세](backend-ai/api-spec.md): baseline·명세 분석·결과 통합·개인화 대화의 입출력과 담당 경계.
- [호출 순서와 curl 예제](backend-ai/api-call-flow.md), [합의 상태·미검증 항목](backend-ai/review-status.md).
- 고도화까지 호출 형태를 합의하고 구현은 baseline부터 진행합니다. 이전 baseline 문서와 달라지는 수동 재시도·공유 동시 처리 규칙은 통합 명세에 명시했으며, 팀 합의 전입니다.
- 합성 문서 검사: `python3 contracts/backend-ai/verify_contract.py --baseline-dir contracts/baseline` (기존 `contracts/requirements-test.txt` 의존성 사용).
