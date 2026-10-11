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
