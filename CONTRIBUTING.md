# 협업 안내

## 변경 공유

일반 개발은 `feat/<작업명>`, `fix/<작업명>`, `docs/<작업명>` 등의 작업 브랜치에서 진행하고 PR로 공유하는 것을 권장합니다.
PR에는 변경 이유, 영향받는 파트, 실행·검증 결과를 적습니다.
이번 초기 디렉토리 구성은 사용자의 명시적 요청에 따라 `main`에 직접 반영합니다.
이 문서는 브랜치 보호나 CI를 설정하지 않으며, 정식 리뷰 규칙과 담당은 팀이 합의해 추가합니다.

## 파일 관리

- 디렉토리는 담당자 이름 대신 코드의 역할을 기준으로 구분합니다.
- STT·TTS 외부 API 연동은 `backend/`에서 시작하고, 실행 위치가 바뀌면 관련 결정을 기록합니다.
- 서비스가 사용하는 AI 코드는 `ai/`, 비교·평가 실행은 `experiments/`에 둡니다.
- API 명세의 관리 원본과 변경 절차는 `contracts/`에서 합의합니다.
- 개인 대화·녹음·전사문, 계정 정보, 실제 `.env`, 모델 가중치·체크포인트는 커밋하지 않습니다.
- 설정 예시는 `.env.example`에 변수 이름과 가짜 값만 적습니다.
- 공유할 데이터는 합성 예제 또는 공유 권한이 확인된 자료로 제한하고 출처·준비 방법을 기록합니다.

## 커밋 예시

`feat: add text chat screen`

`fix: handle speech API timeout`

`docs: define chat response contract`

`chore: initialize project structure`
