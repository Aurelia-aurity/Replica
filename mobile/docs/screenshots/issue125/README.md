# 이슈 #125 인증 메일 재전송 화면

SC-01 로그인 화면을 390×844 논리 크기에서 Flutter 위젯으로 렌더링한 이미지입니다(2배 해상도). 실제 기기 캡처가 아닙니다. 합성 이메일 `new.synthetic@example.com`과 모의 인증 서비스를 사용했으며, 실제 메일 발송·Supabase 응답·요청 제한 값을 확인한 화면이 아닙니다.

| 파일 | 확인 가능한 화면 |
| --- | --- |
| `signup-verification.png` | 가입 결과가 이메일 인증 필요일 때 로그인 화면으로 돌아온 안내와 ‘인증 메일 다시 보내기’. 비밀번호 칸의 ‘비밀번호를 입력해 주세요.’는 가입 뒤 비밀번호를 비우는 기존 동작이며 이번 변경과 무관합니다([이슈 #8 캡처](../issue8/README.md)의 `signup-preview-result.png`와 같음) |
| `login-unverified.png` | 미인증 계정의 로그인 오류와 ‘인증 메일 다시 보내기’ |
| `resend-pending.png` | 재전송 요청 중. 버튼이 ‘인증 메일 요청 중…’으로 바뀌고 입력칸·로그인·회원가입 전환이 비활성 |
| `resend-sent.png` | 재전송 요청 성공 안내 |
| `resend-rate-limited.png` | 요청 제한 안내. 버튼은 다시 누를 수 있음 |

Windows에서 다시 만들기: `flutter test --no-pub test/auth_resend_capture_test.dart --dart-define=AUTH_UI_CAPTURES=true`. 캡처 모드는 Windows 맑은 고딕(앱 테마를 바꾸지 않도록 기본 글꼴 이름으로 등록)과 `C:/flutter`의 Material Icons를 불러옵니다. 일반 테스트에서는 같은 흐름만 실행하고 이미지를 쓰지 않으며 해당 파일에 의존하지 않습니다.

기기 글꼴·실제 키보드·Android/iOS 화면은 이 이미지로 확인하지 않았습니다.
