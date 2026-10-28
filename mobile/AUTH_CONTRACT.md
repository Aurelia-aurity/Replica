# 프론트엔드 인증 결과·오류·401 처리 계약

이슈 #8의 프론트엔드 준비 작업입니다. 파일은 `mobile/`에만 추가·수정하며 앱 시작점은 개발 환경의 명시적인 preview 설정에서만 더미 인증을 허용합니다. HTTP 요청, DB·AI 구현, 토큰 저장 정책은 추가하지 않습니다.

## 가입 요청과 결과

`AuthService.register(SignUpRequest)`는 이메일·비밀번호·`personalRecordsConsent`를 담은 객체를 받습니다. 동의가 없으면 구현체를 호출하기 전에 `consentRequired` 오류로 거부합니다. 구현체는 `performRegistration(request)`에서 요청 객체 전체를 받으므로 동의가 자격 증명과 별도로 유실되지 않습니다. 화면과 더미 서비스는 비밀번호·동의를 저장하지 않습니다. 실제 동의 저장 위치와 방식은 미정입니다.

| 프론트엔드 결과 | 화면 처리 |
| --- | --- |
| `signedIn` | 서비스가 인증 상태를 갱신하고 인증 게이트가 홈을 표시 |
| `emailVerificationRequired` | 로그인 화면으로 전환하고 이메일 인증 안내 |
| `loginRequired` | 로그인 화면으로 전환하고 로그인 안내. 더미에서는 실제 계정이 만들어지지 않았음을 안내 |

결과 타입은 서버의 JSON 규격이 아닙니다. 추후 연동 구현이 SDK 또는 서버 응답을 변환합니다. 더미의 `registrationResult` 설정으로 세 가지 흐름을 각각 재현할 수 있습니다.

## 오류 계약

`AuthFailureKind`는 `invalidCredentials`, `emailNotVerified`, `emailAlreadyRegistered`, `weakPassword`, `rateLimited`, `network`, `consentRequired`, `generic`을 구분합니다. UI는 종류별로 안전한 안내를 표시하며 네트워크 오류는 하단 안내·재시도를, 중복 이메일은 이메일 아래 안내·로그인 전환을 제공합니다. 그 외 인증 실패도 입력을 유지합니다.

더미의 `signInFailure`와 `registrationFailure`로 오류 종류를 주입할 수 있습니다. 기존 미연결 SDK 어댑터도 새 가입 타입과 오류 종류를 따르도록 맞췄습니다. 중복 이메일은 SDK가 실제 중복 오류를 반환한 경우에만 변환하며 성공 형태의 응답으로 중복 여부를 추측하지 않습니다. 어댑터에서 동의를 서버에 저장하는 로직은 추가하지 않습니다.

## 401 공통 처리

`BackendService`는 현재 로그인 세션에 묶인 **프론트엔드 요청 조정자**입니다. 네트워크 클라이언트가 아니며 주입된 더미 또는 향후 전송 함수를 실행합니다. `SessionChatService`가 기존 대화 서비스와 이 조정자를 연결합니다.

1. 요청 시작 시 로그인 상태와 세션 세대를 확인합니다. 로그아웃 후 이전 조정자로 새 요청을 보내는 것도 차단합니다.
2. 추후 전송 계층은 HTTP 상태와 응답을 `BackendFailure.fromResponse`에 전달할 수 있습니다. 현재는 테스트에서 합성 응답을 전달합니다.
3. 현재 세션 요청의 HTTP 401은 `UNAUTHENTICATED`, `retryable: false`로 변환하고 로그인 상태를 해제합니다. 응답이 다른 재시도 값을 주장해도 자동 재전송하지 않습니다.
4. 기존 인증 게이트가 계정별 홈·대화·초안·시트·뒤로 이동 경로를 제거하고 로그인 화면에 만료 안내를 표시합니다.
5. 같은 세션에서 여러 401이 도착해도 한 번만 상태를 해제합니다. 이전 세션의 늦은 401은 새 계정 또는 같은 계정의 재로그인을 해제하지 않습니다. 이전 세션의 늦은 성공 응답도 폐기합니다.

네트워크 오류·403은 로그인 상태를 해제하지 않으며 자동 재시도하지 않습니다. 전송 취소 자체를 구현한 것은 아니고, 이전 세션의 결과가 화면에 반영되지 않도록 하는 처리입니다. 기존 미연결 SDK 어댑터는 401로 해제된 화면을 갱신 이벤트로 재개하지 않고 명시적인 로그인에서 다시 허용합니다.

## 참고한 초안과 한계

[docs/40-api-spec의 앱↔백엔드 API 명세](https://github.com/Aurelia-aurity/Replica/blob/docs/40-api-spec/contracts/app-backend/api-spec.md)는 합의 전 초안입니다. 이 작업은 그 문서의 401 오류 형태를 더미에서 처리하도록 준비한 것이며, 서버에 인증·RLS가 구현됐다는 뜻이 아닙니다.

실제 Supabase 환경, 토큰 저장·갱신 정책, 백엔드 인증 검사·RLS, 동의 저장, 기기 실행은 통합 단계에 남깁니다. 기존 SDK 어댑터 검사도 모의 HTTP 응답만 사용합니다. SDK 가입 응답 형태 참고: [공식 Flutter signUp 문서](https://supabase.com/docs/reference/dart/auth-signup).

검증은 `flutter analyze --no-pub`, `flutter test --no-pub`로 수행합니다. `auth_contract_test.dart`는 가입 세 가지 결과, 동의 전달, 오류별 표시, 중복·지연 401, 이전 응답 폐기, 재로그인과 네트워크/403의 상태 유지를 검사합니다.

2026-10-15 결과: 정적 분석 통과, 전체 테스트 48개 통과. 실제 인증 환경·DB 권한·기기 UI 검증은 포함하지 않습니다.
