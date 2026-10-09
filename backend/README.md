# Backend

FastAPI를 계획 기준으로 사용자·기록·대화 API와 외부 STT·TTS 연동을 관리합니다.

- `app/`: 서비스 코드
- `tests/`: 백엔드 테스트
- `.env.example`: 비밀 값이 없는 설정 예시

HTTP API는 아직 이 브랜치에 구현하지 않았습니다. DB 저장·조회 모듈은 `app/db/`에 구현했습니다.
외부 음성 API 연동은 이 서비스에서 시작하며, 별도의 음성 서버는 만들지 않습니다.
앱 및 AI 서버 연동 형식은 `../contracts/`, DB 변경은 `../infra/supabase/`에서 관리합니다.

## Python DB 모듈

- Python 3.11 이상. 검증 환경: Python 3.12.14.
- 표준 라이브러리만 사용하므로 별도 패키지 설치나 의존성 lock 파일이 필요하지 않습니다.
- [함수 인자·반환값·호출 예시](../docs/architecture/db-python-api.md)
- [구현 이슈 #26](https://github.com/Aurelia-aurity/Replica/issues/26), [DB 설계 이슈 #25](https://github.com/Aurelia-aurity/Replica/issues/25)

서버 환경변수는 `.env.example`의 이름을 사용합니다. `.env` 파일을 자동으로 읽지는 않으므로 실행 환경/Docker/FastAPI 설정에서 환경변수로 주입하세요. 실제 키나 사용자 토큰을 Git·로그·채팅에 넣지 않습니다.

`backend/`에서 호출 예시:

```python
from app.db import Database, Settings

database = Database(Settings.from_env())  # 프로세스 시작 시 생성

async def load_personas(access_token: str):
    user_db = await database.for_user(access_token)  # 각 HTTP 요청에서 토큰 확인
    return await user_db.list_personas(limit=20)
```

`user_db`는 요청별 객체이며 전역에 저장하거나 다른 사용자/요청과 공유하지 않습니다. FastAPI 라우트에서 로그인 토큰을 추출해 전달하는 부분은 API 담당자가 연결해야 합니다.

## 테스트

`backend/`에서 실행:

```text
python -m unittest discover -s tests -v
```

오프라인 테스트는 가짜 Auth/REST 응답과 로컬 HTTP 서버를 사용하며 실제 Supabase 데이터를 변경하지 않습니다. 선택적 실제 연결 시험은 기본적으로 skip합니다.

실제 연결 시험은 `SUPABASE_*`를 환경변수로 설정하고 `REPLICA_DB_LIVE_TEST=1`을 추가한 다음 같은 명령을 실행합니다. Replica 프로젝트 URL에서만 실행하며, 공개 키의 Auth 접근·9개 테이블의 서버 조회·읽기 전용 검색 RPC·비로그인 접근 차단·잘못된 토큰 거부를 검사합니다. `REPLICA_DB_TEST_ACCESS_TOKEN`도 제공하면 실제 로그인 사용자 확인·자기 데이터 조회를 추가 검사합니다. 토큰이 없으면 사용자 검사는 skip하고 서버 검사는 계속합니다. 쓰기/파일/계정 삭제 시험은 하지 않습니다.

2026-10-09: 로컬 `.env`의 값을 출력하지 않고 검사 프로세스에만 주입해 34개 오프라인 검사와 5개 실제 연결 검사가 통과했습니다. 실제 사용자 토큰이 없어 로그인 사용자 검사는 1개 skip했습니다. `.env` 자동 로딩을 서비스 코드에 추가한 것은 아닙니다.

Windows의 제한된 실행 환경에서는 asyncio가 사용하는 내부 loopback 연결이 차단될 수 있습니다. 이 경우 정상 터미널 또는 내부 연결이 허용된 테스트 환경에서 실행하세요.
