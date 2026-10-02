# Backend

FastAPI를 계획 기준으로 사용자·기록·대화 API와 외부 STT·TTS 연동을 관리합니다.

- `app/`: 서비스 코드
- `tests/`: 백엔드 테스트
- `.env.example`: 비밀 값이 없는 설정 예시

현재는 골격만 있습니다. Python 버전, 의존성, 실행·테스트 방법은 초기 구현 시 추가합니다.
외부 음성 API 연동은 이 서비스에서 시작하며, 별도의 음성 서버는 만들지 않습니다.
앱 및 AI 서버 연동 형식은 `../contracts/`, DB 변경은 `../infra/supabase/`에서 관리합니다.
