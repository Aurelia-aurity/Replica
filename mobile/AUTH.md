# 이슈 #8: 프론트엔드 인증

사용자 요청에 따라 **mobile/만 구현**합니다. 백엔드 서버, DB 테이블, RLS 정책, 원격 프로젝트 설정은 변경하지 않습니다. 실제 인증 환경 검증과 백엔드 담당 합의는 후속 작업입니다.

## PR #24와 브랜치 관계

- #4의 [PR #24](https://github.com/Aurelia-aurity/Replica/pull/24)는 미병합 초안입니다.
- 로컬 작업 브랜치: `codex/8-email-auth-session`.
- 시작 커밋: PR #24의 `2b60ebaf4b98e0dbc3019700f7bd0152d3f8216d`.
- #8 변경만 확인: `git diff feat/4-flutter-chat-scaffold -- mobile/`.
- PR #24 브랜치에는 #8 변경을 커밋하거나 푸시하지 않습니다. #24 병합 전 main과 비교하면 #4 변경도 함께 보입니다. 추후 PR은 #24 브랜치를 비교 기준으로 삼거나, #24 병합 후 main 위로 #8 변경을 재배치합니다.
- `main.dart`에 인증 게이트를 추가하고 홈의 계정 버튼에 로그아웃을 연결합니다. 기존 `ChatService`는 데모로 유지합니다.
- 대화 진입 테스트에서 기존 AI 안내 시트의 `DecoratedBox`가 `CheckboxListTile`의 Material 배경을 가리는 Flutter 오류를 발견해 배경을 `Material`로 최소 수정했습니다.

## 연결 없이 화면 확인

```powershell
cd mobile
flutter pub get
flutter run -d chrome --dart-define-from-file=config/dev.local.json
# Android 에뮬레이터
flutter run -d <기기ID> --dart-define-from-file=config/dev.android.json
```

개발 설정은 `AUTH_MODE=preview`입니다. staging/prod에서는 preview를 거부합니다. 미리보기는 실제 인증·회원 생성·메일 발송·서버 통신을 하지 않습니다.

| 합성 시험 계정 이메일 | 비밀번호 |
| --- | --- |
| preview.a@example.com | Replica123! |
| preview.b@example.com | Replica123! |

가입 화면에서는 입력 검사와 완료 안내만 확인하며 계정을 생성하지 않습니다. 홈의 계정 버튼에서 로그아웃 또는 ‘세션 만료 확인 (미리보기)’를 누르면 로그인 화면으로 돌아갑니다. 대화 도중 만료와 늦게 도착한 응답은 자동 테스트로 검사합니다.

## 추후 Supabase 연결

`config/auth.example.json`의 URL과 **publishable key**를 개발 프로젝트 값으로 교체한 `config/auth.local.json`을 사용합니다. 예시 그대로 실행하면 설정 검증이 거부합니다. 서비스 키·secret key·계정 비밀번호를 앱 설정에 넣지 않습니다.

```powershell
flutter run -d <기기ID> --dart-define-from-file=config/auth.local.json
```

`AuthService`는 화면용 추상 인터페이스이고 `SupabaseAuthService`는 고정 버전 `supabase_flutter 2.18.0` SDK 어댑터입니다. 로그인·가입·로그아웃·갱신 이벤트·만료 타이머·안전한 오류 안내를 연결했습니다. Android 인터넷 권한을 추가했으며 HTTPS 인증 주소를 사용합니다.

이메일 확인이 필요한 가입은 세션이 없으면 홈을 열지 않습니다. 인증 링크를 외부 브라우저에서 확인한 뒤 앱으로 돌아와 비밀번호로 로그인합니다. 프로젝트 Site URL은 유효한 확인 완료 페이지로 설정해야 합니다. 자동 앱 복귀/deep link, 비밀번호 재설정, MFA, CAPTCHA는 이번 범위에 없습니다.

현재 세션은 메모리에만 보관하고 SDK 자동 갱신을 사용합니다. 앱 재시작 후에는 다시 로그인합니다. 기기 보안 저장소를 통한 영구 세션 복원은 후속 작업입니다. 인증 설정이 없으면 로그인 버튼을 비활성화하고 홈을 열지 않습니다.

## 계정 데이터와 백엔드 연결 지점

로그아웃·만료·사용자 변경 시 사용자별 Navigator와 ScaffoldMessenger를 폐기합니다. 대화·입력 초안·AI 안내 시트와 이전 화면으로 돌아가는 경로가 함께 제거됩니다. 폐기된 대화 화면에 늦게 도착한 응답은 기존 mounted 검사로 무시합니다. 같은 사용자에 대한 정상 토큰 갱신은 대화 화면을 유지합니다.

추후 HTTP 클라이언트는 요청마다 `await auth.authorizationHeaders()`를 호출해 최신 `Authorization: Bearer <access token>` 헤더를 얻을 수 있습니다. 만료 임박 시 갱신하며 갱신 도중 계정 변경 시 전달을 거부합니다. 미리보기는 헤더를 생성하지 않습니다. 현재 이 메서드를 호출하는 서버 요청은 없습니다.

백엔드 연결 시 합의할 사항: HTTPS 요청 주소, 검증된 토큰의 사용자 ID를 통한 소유권 검사, 401/403 오류 계약과 재인증, 사용자별 캐시·요청 취소 범위, 서버 로그아웃 정책. 갱신 토큰·비밀번호는 백엔드에 보내지 않습니다. 화면 접근 제어로 서버 권한 검사나 RLS 검증을 대체할 수 없습니다.

## 검증과 남은 범위

```powershell
dart format --output=none --set-exit-if-changed lib/auth lib/main.dart test/auth_test.dart test/supabase_auth_service_test.dart
flutter analyze
flutter test
flutter build web --dart-define-from-file=config/dev.local.json
```

실제 Supabase 환경의 가입·로그인·로그아웃, 이메일 확인, 두 계정 분리, 서버 권한 검사와 RLS는 미검증입니다. Android/iOS 기기 실행도 별도로 필요합니다. 이슈 #8 전체 완료 또는 종료로 표시하지 않습니다. 현재 결과는 `AUTH_VALIDATION.md`에 기록합니다.

공식 참고: [Auth 이벤트](https://supabase.com/docs/reference/dart/auth-onauthstatechange), [회원가입](https://supabase.com/docs/reference/dart/auth-signup), [Flutter SDK](https://pub.dev/packages/supabase_flutter).
