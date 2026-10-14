# 이슈 #8: 프론트엔드 인증

사용자 요청에 따라 **mobile/만 구현**합니다. 백엔드 서버, DB 테이블, RLS 정책, 원격 프로젝트 설정은 변경하지 않습니다. 실제 인증 환경 검증과 백엔드 담당 합의는 후속 작업입니다.

## PR #24와 브랜치 관계

- #4의 [PR #24](https://github.com/Aurelia-aurity/Replica/pull/24)는 미병합 초안입니다.
- 로컬 작업 브랜치: `feat/8-email-auth-session`.
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

2026-10-13 SC-01 반영 이후 앱 시작점은 설정과 관계없이 더미 `PreviewAuthService`만 주입합니다. 실제 인증·회원 생성·메일 발송·서버 통신을 하지 않습니다. 이전 `AuthConfig`와 SDK 어댑터는 앱 실행 경로에 연결하지 않습니다. 화면 동작과 기획서 대응은 [SC01.md](SC01.md)를 참고하세요.

| 합성 시험 계정 이메일 | 비밀번호 |
| --- | --- |
| preview.a@example.com | Replica123! |
| preview.b@example.com | Replica123! |

가입 화면에서는 이메일·비밀번호·비밀번호 확인·필수 개인 기록 처리 동의를 검사하고 완료 안내만 표시합니다. 계정을 생성하거나 동의를 저장하지 않습니다. 홈의 계정 버튼에서 로그아웃 또는 ‘세션 만료 확인 (미리보기)’를 누르면 로그인 화면으로 돌아갑니다. 대화 도중 만료와 늦게 도착한 응답은 자동 테스트로 검사합니다.

## 추후 실제 인증 연결

현재 실행은 더미 인증만 제공합니다. `config/auth.example.json`, `AuthConfig`, 기존 SDK 어댑터는 이전 준비 코드이며 현재 앱 시작점에서는 사용하지 않습니다. 설정 파일을 바꾸어도 실제 인증으로 전환되지 않습니다.

백엔드 담당과 인증 및 오류 계약을 합의한 뒤 `ReplicaApp`에 실제 `AuthService`를 주입하는 방식으로 교체합니다. SC-01은 가입 시 `register` 계약에 이메일·비밀번호·개인 기록 처리 동의를 전달하며 화면에서 저장하거나 통신하지 않습니다.

실제 세션 영구 저장, 인증 메일과 앱 복귀, 계정별 서버 권한 검사·RLS는 해당 담당자의 구현과 통합 검증에서 처리합니다.
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
