# 프론트엔드 인증 결과·오류·401 처리 계약

앱은 개발 환경의 preview 모드에서 더미 인증을 사용하고, Supabase 모드에서는 프로젝트 설정을 받아 SDK를 초기화합니다. 실행 설정은 [인증 안내](AUTH.md)를 참고하세요.

## 가입 요청과 결과

`AuthService.register(SignUpRequest)`는 이메일·비밀번호·`personalRecordsConsent`를 받습니다. 동의가 없으면 구현체를 호출하기 전에 `consentRequired` 오류로 거부합니다. Supabase 어댑터는 동의 값을 가입 요청의 사용자 메타데이터로 전달합니다. 정식 동의 기록의 저장 계약은 미정입니다.

| 프론트엔드 결과 | 화면 처리 |
| --- | --- |
| `signedIn` | 서비스가 인증 상태를 갱신하고 인증 게이트가 홈을 표시 |
| `emailVerificationRequired` | 로그인 화면으로 전환하고 이메일 인증 안내 |
| `loginRequired` | 로그인 화면으로 전환하고 로그인 안내. 더미에서는 실제 계정이 만들어지지 않았음을 안내 |

결과 타입은 서버의 JSON 규격이 아닙니다. Supabase 어댑터가 SDK 가입 응답을 변환합니다. 세션이 있으면 `signedIn`, 없으면 `emailVerificationRequired`입니다. 더미의 `registrationResult` 설정으로 세 가지 흐름을 각각 재현할 수 있습니다.

## 오류 계약

`AuthFailureKind`는 `invalidCredentials`, `emailNotVerified`, `emailAlreadyRegistered`, `weakPassword`, `rateLimited`, `network`, `consentRequired`, `generic`을 구분합니다. UI는 종류별로 안전한 안내를 표시하며 네트워크 오류는 하단 안내·재시도를, 중복 이메일은 이메일 아래 안내·로그인 전환을 제공합니다. 그 외 인증 실패도 입력을 유지합니다.

더미의 `signInFailure`와 `registrationFailure`로 오류 종류를 주입할 수 있습니다. Supabase 어댑터는 SDK가 실제 중복 오류를 반환한 경우에만 중복 이메일 오류로 변환합니다. 오류 코드 없이 HTTP 429가 와도 `rateLimited`로 변환합니다.

## 인증 메일 재전송 (#125)

`AuthService.resendSignUpConfirmation(email)`은 가입 확인 메일을 다시 요청합니다. Supabase 어댑터는 SDK의 `auth.resend(type: OtpType.signup, email, emailRedirectTo)`를 호출하며, 가입과 같은 `SUPABASE_EMAIL_REDIRECT_TO`를 씁니다. 실패는 로그인·가입과 같은 `AuthFailure` 변환을 거칩니다(`over_email_send_rate_limit`·`over_request_rate_limit`·429는 `rateLimited`, 연결 실패와 5xx는 `network`). 더미는 메일을 보내지 않고 성공하며, `resendFailure`로 오류 종류를 주입할 수 있습니다.

| 버튼이 나오는 경우 | 재전송 대상 |
| --- | --- |
| 가입 결과가 `emailVerificationRequired` | 가입 요청에 보낸 이메일(앞뒤 공백 제거) |
| 로그인이 `emailNotVerified`로 거절됨 | 로그인 요청에 보낸 이메일(앞뒤 공백 제거) |

- 재전송 대상은 위 요청 때 정해지며 이메일 입력칸의 이후 값을 보내지 않습니다. 입력칸을 다른 주소로 바꾸거나, 로그인·회원가입을 전환하거나, 새로 로그인·가입을 요청하면 버튼과 결과 안내가 사라집니다.
- 요청 중에는 재전송 버튼을 비활성화하고(‘요청 중…’), 입력칸·로그인·회원가입 전환도 막아 요청이 겹치지 않게 합니다. 로그인·가입을 새로 요청하면 그 순간 버튼이 사라지므로, 로그인·가입 요청과 재전송이 함께 진행되지 않습니다.
- 결과는 버튼 아래에 표시합니다. 성공: ‘인증 메일을 다시 요청했어요.’ 요청 제한: ‘잠시 후 다시 시도해 주세요.’ 연결 실패: ‘네트워크 연결을 확인하고 다시 시도해 주세요.’ 그 밖의 실패: ‘요청하지 못했어요. 다시 시도해 주세요.’ 실패 뒤에도 버튼을 다시 누를 수 있습니다. 서버 오류 원문은 표시하지 않습니다.
- Supabase는 이메일 존재 여부를 드러내지 않으려고 없는 주소나 이미 인증한 주소에도 성공으로 답할 수 있습니다. 그래서 성공 안내는 ‘보냈어요’가 아니라 ‘요청했어요’로 표현합니다.
- 앱은 재전송 간격(쿨다운)을 따로 두지 않고 서버의 요청 제한 응답을 따릅니다. Supabase의 기본 이메일 발송 제한·간격은 프로젝트 설정에 따르며 실제 값은 확인하지 않았습니다.

## 401 공통 처리

`BackendService`는 현재 로그인 세션에 묶인 **프론트엔드 요청 조정자**입니다. 네트워크 클라이언트가 아니며 주입된 전송 함수(미리보기 메모리 서비스 또는 HTTP 대화 서비스)를 실행합니다. `SessionChatService`와 대화 이력 서비스가 이 조정자를 사용합니다.

1. 요청 시작 시 로그인 상태와 세션 세대를 확인합니다. 로그아웃 후 이전 조정자로 새 요청을 보내는 것도 차단합니다.
2. 대화 HTTP 클라이언트는 HTTP 상태와 응답을 `BackendFailure.fromResponse`에 전달합니다.
3. 현재 세션 요청의 HTTP 401은 `UNAUTHENTICATED`, `retryable: false`로 변환하고 로그인 상태를 해제합니다. 응답이 다른 재시도 값을 주장해도 자동 재전송하지 않습니다.
4. 기존 인증 게이트가 계정별 홈·대화·초안·시트·뒤로 이동 경로를 제거하고 로그인 화면에 만료 안내를 표시합니다.
5. 같은 세션에서 여러 401이 도착해도 한 번만 상태를 해제합니다. 이전 세션의 늦은 401은 새 계정 또는 같은 계정의 재로그인을 해제하지 않습니다. 이전 세션의 늦은 성공 응답도 폐기합니다.

네트워크 오류·403은 로그인 상태를 유지합니다. 401 처리 시 SDK의 로컬 세션도 지우며, 사용자는 서버 로그아웃 요청이 진행 중이어도 로컬 정리 후 다시 로그인할 수 있습니다.

## 참조 명세와 한계

현재 참조 문서는 저장소의 [앱 ↔ 백엔드 API 명세](../contracts/app-backend/api-spec.md)입니다. 명세의 구현·제안·미정 상태를 구분해 사용합니다. 인증 계약은 Supabase JWT의 Bearer 헤더이며 로그인·회원가입 API는 백엔드에 없습니다. 앱은 Supabase Auth와 직접 로그인·회원가입하고, 백엔드 요청에는 `authorizationHeaders()`로 얻은 `Authorization: Bearer <access token>`만 보냅니다. 명세의 401 `UNAUTHENTICATED`(`retryable: false`)는 응답 본문과 관계없이 HTTP 상태로 판정합니다. 이 문서의 401 검증은 더미·모의 응답으로 수행했으며 서버 인증·RLS를 검증한 결과가 아닙니다.

세션 저장·복원과 자동 갱신은 Supabase Flutter SDK를 따릅니다. 실제 Supabase 프로젝트와 백엔드를 함께 쓰는 통합 검증, 백엔드 인증 검사·RLS, 정식 동의 저장, 기기 실행 확인은 남아 있습니다. SDK 어댑터 테스트는 모의 HTTP 응답만 사용합니다. SDK 가입 응답 형태 참고: [공식 Flutter signUp 문서](https://supabase.com/docs/reference/dart/auth-signup).

검증은 `flutter analyze --no-pub`, `flutter test --no-pub`로 수행합니다. `auth_contract_test.dart`는 가입 세 가지 결과, 동의 전달, 오류별 표시, 중복·지연 401, 이전 응답 폐기, 재로그인과 네트워크/403의 상태 유지를 검사합니다.

2026-10-15 결과: 정적 분석 통과, 전체 테스트 48개 통과. 실제 인증 환경·DB 권한·기기 UI 검증은 포함하지 않습니다.
