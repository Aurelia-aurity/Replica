# Replica Mobile

SC-09 텍스트 대화, SC-10 음성 대화, SC-11 대화 이력의 화면과 API 호출 코드를 구현했습니다. 제공된 개발 설정은 더미 인증·메모리 데이터를 사용하는 미리보기입니다. Supabase 초기화·실제 로그인 진입점은 로그인 통합 작업에서 담당합니다.

- [SC09.md](SC09.md): 근거 기록·근거 없음 표시, 응답 메타데이터, 턴별 TTS 호출과 재생 안내
- [SC10.md](SC10.md): 마이크 권한, 녹음·음성 API 호출, 답변·음성 재생과 검증 범위
- [VOICE_VALIDATION.md](VOICE_VALIDATION.md): 실제 음성 계약·중단 처리·실기기 10회 측정 절차와 미검증 항목
- [SC11.md](SC11.md): 홈 최근 대화·날짜별 목록, 이어가기·삭제·재시도와 API 연동 한계

미리보기의 대화는 이번 로그인 동안 메모리에 보관하며, 앱 재시작·로그아웃·계정 전환 시 초기화됩니다. 마이크와 실제 음성 합성·출력을 사용하지 않습니다.

SC-01 로그인·회원가입 기획서의 최신 반영 내용은 [SC01.md](SC01.md)에 있습니다. 현재 앱 실행은 프론트엔드 더미 인증만 사용하며 실제 서버에 연결하지 않습니다.

추후 연동을 위한 가입 결과·인증 오류·401 처리와 더미 검증은 [AUTH_CONTRACT.md](AUTH_CONTRACT.md)를 참고하세요.

이슈 #4의 Flutter 앱 기반과 대화 화면에 이슈 #8의 프론트엔드 인증 흐름을 추가했습니다. 합성 시험 계정과 인증 검증 범위는 [AUTH.md](AUTH.md)를 참고하세요.

## 개발 환경

- Flutter **3.47.5 stable** (revision `6a19cca564`)
- Dart **3.13.4** (위 Flutter SDK에 포함)
- Android: Android Studio, Android SDK, 에뮬레이터 또는 USB 디버깅 기기
- iOS: macOS, Xcode, iOS 시뮬레이터 또는 서명 가능한 기기

저장소 루트에서 아래 명령을 실행합니다.

```powershell
cd mobile
flutter --version
flutter doctor -v
flutter pub get
flutter devices
```

`flutter doctor -v`에서 사용하는 플랫폼의 도구 설치 상태를 확인하세요.

## 실행과 환경 설정

Android Studio에서 `mobile`을 프로젝트로 열면 기본 `main.dart` 실행은 `config/auth.local.json`을 사용합니다. `main.dart (더미)`를 선택하면 기존 미리보기 계정으로 실행합니다. 공유 실행 설정은 `.run/`에 있습니다.

`config/auth.local.json`이 없으면 `config/auth.example.json`을 복사하고 Supabase URL·publishable key·백엔드 주소를 입력하세요. 로컬 설정 파일은 Git에서 제외됩니다.

```powershell
# 실제 인증
flutter run -d <기기ID> --dart-define-from-file=config/auth.local.json

# 더미 실행. Android 에뮬레이터 실행 후 flutter devices에서 기기 ID 확인
flutter emulators
flutter emulators --launch <에뮬레이터ID>
flutter run -d <기기ID> --dart-define-from-file=config/dev.android.json

# 더미 실행. iOS 시뮬레이터 / Chrome / Windows
flutter run -d <기기ID> --dart-define-from-file=config/dev.local.json
```

| 설정 | 용도 | 백엔드 주소 |
| --- | --- | --- |
| `config/auth.local.json` | 실제 Supabase 인증, 로컬 전용 | 시험 백엔드 주소 입력 |
| `config/dev.android.json` | Android 에뮬레이터에서 호스트 PC 접근 | `http://10.0.2.2:8000` |
| `config/dev.local.json` | iOS 시뮬레이터·웹·데스크톱 | `http://localhost:8000` |
| `config/staging.example.json` | 스테이징 설정 예시 | HTTPS 예시 주소 |
| `config/prod.example.json` | 운영 설정 예시 | HTTPS 예시 주소 |

더미 설정인 `dev.android.json`과 `dev.local.json`은 `APP_ENV=dev`, `AUTH_MODE=preview`를 명시합니다. 실제 인증 설정은 `AUTH_MODE=supabase`를 사용합니다. URL과 키가 모두 비어 있으면 인증 미설정 화면을 표시하며, 유효한 설정을 입력하면 Supabase SDK를 초기화합니다.

실제 휴대폰에서는 localhost가 휴대폰 자체를 가리킵니다. PC와 같은 네트워크의 PC 주소로 덮어쓰세요.

```powershell
flutter run -d <기기ID> --dart-define=APP_ENV=dev --dart-define=AUTH_MODE=preview --dart-define=BACKEND_BASE_URL=http://192.168.0.10:8000
```

스테이징·운영 예시의 도메인은 실제 서버가 아닙니다. 실제 HTTPS 주소를 지정해야 합니다.

```powershell
flutter run -d <기기ID> --dart-define=APP_ENV=prod --dart-define=BACKEND_BASE_URL=https://your-api.example.com
```

`APP_ENV`는 `dev`, `staging`, `prod`만 허용하며 스테이징·운영에서는 HTTPS를 요구합니다. 설정은 빌드 시 반영되므로 변경 후 앱을 다시 실행해야 합니다. URL에 인증 정보·쿼리·프래그먼트를 넣지 마세요. 앱 설정에 비밀 API 키를 넣지 않습니다. Android 디버그 빌드는 로컬 HTTP 백엔드를 허용합니다.

미리보기에서는 `MemoryConversationService`를 사용합니다. 실제 인증 모드로 로그인하면 `AuthGate`가 `HttpConversationService`를 선택하고 설정된 백엔드 주소에 JWT를 전달합니다. 서버 통합 검증과 Android 네트워크 정책·iOS ATS·웹 CORS는 해당 실행 환경에서 확인해야 합니다.

## 현재 화면과 동작

화면·연동 참조 문서는 [화면 기획서 ver.2](<../docs/requirements/레플리카 화면 기획서_ver.2.pdf>)와 저장소의 [앱 ↔ 백엔드 API 명세](../contracts/app-backend/api-spec.md)입니다. API의 구현·제안·미정 상태는 해당 명세를 따릅니다. 아래는 현재 구현 범위이며 실제 서버 연결 완료를 의미하지 않습니다.

| 화면 | 현재 구현 |
| --- | --- |
| SC-02 홈 | 예시 상대 카드, 텍스트·음성 대화 진입, 최근 대화·대화 이력 탭 연결 |
| SC-08 AI 생성 안내 | 새 대화마다 안내 시트, 이해 확인 체크, 닫기·대화 시작 |
| SC-09 텍스트 대화 | 고정 AI 안내·응답 배지, 입력·전송·오류·재시도·수정, 근거 기록·미제공·근거 없음 구분, TTS·재생 |
| SC-10 음성 대화 | 권한 안내, 녹음·취소·시간 제한, 음성 질문 전송·인식 결과·답변, 처리 상태·응답 시간, 합성 음성 안내·재생·일시정지·위치 이동 |
| SC-11 대화 이력 | 날짜별 목록·상세·이어가기, 새 대화 방식 선택, 삭제 확인·취소·실패 재시도, 빈 목록·로딩·오류 |

앱은 로그인 화면에서 시작합니다. 로그인 후 홈에서 텍스트 또는 음성 대화 버튼을 누르면 AI 안내를 표시하고, 이해 확인 후 선택한 방식으로 새 대화를 엽니다. 안내 시트는 배경 탭·아래로 드래그·뒤로 버튼으로 닫히지 않고 ‘닫기’로만 취소할 수 있습니다. 확인 시각은 미리보기에서 메모리에 유지하며, HTTP 구현에서는 대화방 생성 시 `ai_notice_ack_at`으로 전송합니다. 실제 서버 저장 여부는 통합 검증이 필요합니다.

대화방 안의 텍스트·음성 전환 버튼은 제공하지 않습니다. 홈이나 대화 이력에서 새 대화를 만들 때 방식을 선택합니다. 더보기 버튼으로 AI 생성 안내를 다시 볼 수 있습니다. 기획서의 주황색 번호와 설명 영역은 앱 UI에 포함하지 않습니다.

홈은 `GET /personas`의 활성 대화 상대를 보여 줍니다. 여러 명이면 선택할 수 있고, 선택한 이름과 `persona_id`를 같은 대화방 생성 요청에 사용합니다. 최근 대화와 대화 이력은 현재 사용자의 대화 서비스를 조회합니다. 기록·설정 탭은 향후 기능 안내를 제공합니다. 계정 버튼은 현재 계정과 로그아웃을 제공하며 미리보기에서는 세션 만료를 재현할 수 있습니다.

음성 화면에는 기기 녹음·파형·재생 제어와 서버 호출 경로가 구현되어 있습니다. 미리보기에서는 합성 녹음 데이터와 예시 질문·답변으로 흐름을 확인하며 실제 마이크·STT·TTS·오디오 출력을 사용하지 않습니다. 텍스트 화면의 미리보기 ‘듣기’도 미지원 안내만 표시합니다. 비미리보기 서비스에서 텍스트 답변을 들을 때는 최초·다시 듣기·실패 후 재시도마다 합성 음성 안내에 동의해야 하며, 같은 화면의 캐시 음성은 TTS 재호출 없이 재생합니다.

근거 응답은 `evidence: {status, items}`를 사용합니다. `status=available`일 때 `items[].file_name/date/excerpt`를 SC-09에 보여 주고, `status=none`만 ‘근거 기록 없음’으로 보여 줍니다. 필드가 없거나 `unavailable/unknown`이면 ‘근거 정보 미제공’으로 유지합니다. 현재 `contracts/app-backend` 명세에는 이 응답 필드가 아직 없으므로, 서버가 필드를 추가하기 전에는 미제공 상태가 정상입니다. 기록 관리와 설정·계정 삭제는 후속 작업입니다.

입력창은 키보드가 열리면 최대 두 줄로 줄이고 내부를 스크롤합니다. 텍스트 응답 생성 중에는 중복 전송을 막고, 실패한 메시지에는 다시 보내기 아이콘과 응답 자리의 재시도·수정 버튼을 제공합니다.

## 구조와 동작

- `lib/main.dart`: 앱 시작과 공통 테마
- `lib/home/home_screen.dart`: 홈과 새 대화 진입
- `lib/chat/ai_disclosure_sheet.dart`: AI 안내 및 이해 확인
- `lib/chat/voice_screen.dart`: 음성 녹음·전송·재생 화면
- `lib/shared/conversation_widgets.dart`: 공통 색상과 AI 배지
- `lib/config/app_config.dart`: 빌드 환경·백엔드 주소 파싱 및 검증
- `lib/chat/chat_service.dart`: 텍스트·상세 응답·음성 서비스 인터페이스와 데모 응답
- `lib/chat/chat_screen.dart`: 텍스트 입력·응답, 근거 보기, TTS·재생, 오류·재시도
- `lib/history/conversation_service.dart`: 미리보기 메모리 저장, 인증 HTTP 호출, 재시도 UUID 유지
- `lib/history/history_screen.dart`: 대화 이력·이어가기·삭제·새 대화 생성
- `lib/auth/auth_gate.dart`: 인증 세션별 화면·대화 서비스 분리
- `test/`: 화면 흐름·키보드 영역·환경 설정 검사

입력은 최대 2,000자이며 공백만 있는 메시지는 전송할 수 없습니다. 응답 대기 중에는 중복 전송을 막습니다. 응답 실패·빈 응답·180초 타임아웃(`lib/services/request_timeouts.dart`)은 오류 안내로 처리하고, 같은 메시지 재시도 또는 메시지 수정을 제공합니다. 화면을 닫은 후 응답이 도착해도 상태를 갱신하지 않습니다.

오류 화면은 테스트에서 실패하는 서비스를 주입해 검증합니다. 현재 데모 서비스는 정상 응답만 반환하며 실제 네트워크 오류는 재현하지 않습니다.

## 직접 실행 및 검증

```powershell
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build web --dart-define-from-file=config/dev.local.json
```

최근 자동 검증은 **2026-11-10**, 기준 커밋 `a96b904`에 음성 화면 생명주기·같은 녹음 재시도 소유권의 로컬 미커밋 수정(PR #106 리뷰 1·2번)이 더해진 상태에서 수행했습니다. 전체 Flutter 테스트 **137개**(그중 `test/sc10_test.dart` **35개**)와 `flutter analyze --no-pub`가 통과했습니다. 아래 기록은 검증 당시 코드 상태를 기준으로 하며, 이후 문서 정리만 반영했습니다.

| 기록 날짜 | 기준 코드 | 자동 검증 기록 | 빌드 기록 |
| --- | --- | --- | --- |
| 2026-11-10 | `a96b904` + 음성 생명주기·재시도 소유권 로컬 수정 | 전체 137개·SC-10 테스트 35개, 정적 분석 통과 | 현재 작업 상태에서 재실행하지 않음 |
| 2026-11-04 | [175c242](https://github.com/Aurelia-aurity/Replica/commit/175c24290667b1fab8b33279161c5226dc4c59fe)에 남은 대화 HTTP 연결 검증 기록 | 전체 109개, 정적 분석 통과 | 웹·Android 디버그 APK 성공 기록 |
| 2026-11-04 | [4769eae](https://github.com/Aurelia-aurity/Replica/commit/4769eae4c0b85016b9765b780ecdd354350d6145)에 남은 음성 보완 검증 기록 | 전체 113개·음성 관련 23개, 정적 분석 통과 | Android 디버그 APK 성공 기록 |

109개·113개와 과거 빌드 결과는 위 커밋의 문서에 보관된 기록이며 최신 작업 상태의 결과로 사용하지 않습니다. 자동 검증에는 상대 ID·표시 이름 연결, AI 안내 확인 시각, 근거 상태 매핑, 계정 전환·지연 응답 폐기와 음성 요청의 백그라운드·재시도 경합을 포함합니다. 테스트는 가짜 녹음기·플레이어·HTTP 응답을 사용하며, 실제 인증·배포 서버·RLS·이력 삭제·실기기 음성 입출력·연속 10회 중 9회 성공을 확인한 결과는 아닙니다. Supabase 초기화·실제 로그인 연결은 로그인 통합 작업에 남아 있습니다.

2026-10-29 `4d33095` 기준 웹·Android 디버그 APK 빌드가 통과했습니다. 이전 Android 한글 입력·데모 응답 확인 기록과 캡처는 [검증 기록](VALIDATION.md)에 있습니다. 실제 인증·백엔드 통합, 실기기의 마이크·STT·TTS·출력, iOS/macOS 빌드는 미검증입니다. 추가 환경에서 직접 확인할 항목은 다음과 같습니다.

- 입력창과 전송 버튼이 작은 화면·가로 화면·키보드 열린 상태에서 사용 가능한지
- 긴 입력과 긴 대화 목록에 넘침이 없는지
- 대화 전송 후 최신 메시지가 보이고 키보드를 닫아도 화면이 정상인지
- 기기의 한글 입력·선택·붙여넣기가 정상인지

자동 테스트는 실제 기기의 키보드 동작과 플랫폼 실행 검증을 대체하지 않습니다. 화면별 검증과 남은 API 제약은 SC09·SC10·SC11 문서를 함께 참고하세요.
