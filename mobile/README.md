# Replica Mobile

이슈 #4의 Flutter 앱 기반과 대화 화면입니다. 현재는 **데모 모드**이며, 메시지는 서버로 전송되지 않고 메모리에만 보관됩니다. 앱을 다시 시작하면 대화가 초기화됩니다.

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

```powershell
# Android 에뮬레이터 실행 후 flutter devices에서 기기 ID 확인
flutter emulators
flutter emulators --launch <에뮬레이터ID>
flutter run -d <기기ID> --dart-define-from-file=config/dev.android.json

# iOS 시뮬레이터 / Chrome / Windows
flutter run -d <기기ID> --dart-define-from-file=config/dev.local.json
```

| 설정 | 용도 | 백엔드 주소 |
| --- | --- | --- |
| `config/dev.android.json` | Android 에뮬레이터에서 호스트 PC 접근 | `http://10.0.2.2:8000` |
| `config/dev.local.json` | iOS 시뮬레이터·웹·데스크톱 | `http://localhost:8000` |
| `config/staging.example.json` | 스테이징 설정 예시 | HTTPS 예시 주소 |
| `config/prod.example.json` | 운영 설정 예시 | HTTPS 예시 주소 |

실제 휴대폰에서는 localhost가 휴대폰 자체를 가리킵니다. PC와 같은 네트워크의 PC 주소로 덮어쓰세요.

```powershell
flutter run -d <기기ID> --dart-define=APP_ENV=dev --dart-define=BACKEND_BASE_URL=http://192.168.0.10:8000
```

스테이징·운영 예시의 도메인은 실제 서버가 아닙니다. 실제 HTTPS 주소를 지정해야 합니다.

```powershell
flutter run -d <기기ID> --dart-define=APP_ENV=prod --dart-define=BACKEND_BASE_URL=https://your-api.example.com
```

`APP_ENV`는 `dev`, `staging`, `prod`만 허용하며 스테이징·운영에서는 HTTPS를 요구합니다. 설정은 빌드 시 반영되므로 변경 후 앱을 다시 실행해야 합니다. URL에 인증 정보·쿼리·프래그먼트를 넣지 마세요. 앱 설정에 비밀 API 키를 넣지 않습니다.

**현재 백엔드 주소는 설정 검증만 수행하며 실제 통신에는 사용하지 않습니다.** API 명세가 합의되면 `ChatService` 구현에서 `AppConfig.backendBaseUrl`을 사용해 연결합니다. HTTP 통신 구현 시 Android 인터넷 권한 및 개발용 cleartext 정책, iOS ATS, 웹 CORS 설정도 검증해야 합니다.

## 화면 기획서에 따른 이슈 #4 범위

기획서의 baseline 흐름을 기본 화면으로 구성했습니다.

| 화면 | 이번 구현 |
| --- | --- |
| SC-02 홈 | 예시 상대 카드, 텍스트·음성 대화 진입, 하단 탭 틀 |
| SC-08 AI 생성 안내 | 새 대화마다 안내 시트, 이해 확인 체크, 닫기·대화 시작 |
| SC-09 텍스트 대화 | 고정 AI 안내, 말풍선·응답 배지, 입력·전송, 로딩·오류·재시도 |
| SC-10 음성 대화 | 어두운 화면, 처리 단계·질문·응답 자리, 합성 음성 안내, 키보드 전환 |

앱은 홈에서 시작합니다. 텍스트 또는 음성 대화 버튼을 누르면 AI 안내를 표시하고, 이해 확인 후 선택한 방식으로 새 대화를 엽니다. 안내 시트는 배경 탭·아래로 드래그·뒤로 버튼으로 닫히지 않고 ‘닫기’로만 취소할 수 있습니다. 확인 시각은 대화 화면의 세션 메타데이터로 메모리에 유지하며, 서버 저장은 아직 하지 않습니다.

텍스트 화면의 마이크와 음성 화면의 키보드 버튼은 같은 세션 안에서 화면 방식을 전환합니다. 입력 중인 문장과 대화 내용은 전환 시 유지하고, 홈으로 나가면 세션을 종료합니다. 더보기 버튼으로 AI 생성 안내를 다시 볼 수 있습니다. 기획서의 주황색 번호와 설명 영역은 앱 UI에 포함하지 않습니다.

홈의 상대 이름 ‘엄마’는 예시입니다. 실제 프로필·기록 개수·최근 대화를 임의로 표시하지 않습니다. 홈의 기록·대화 이력·설정 탭과 계정 버튼은 향후 기능 안내만 제공합니다.

음성 화면은 **화면 틀만 구현**했으며 마이크 권한을 요청하거나 녹음하지 않습니다. STT·TTS·음성 재생·파형·재생 시간도 제공하지 않습니다. 텍스트 화면에서 주고받은 최신 질문·응답은 음성 화면에 표시하며, 처리 단계는 아직 진행 상태를 표시하지 않습니다. 듣기와 말하기 버튼은 미구현 안내를 제공합니다.

기획서의 고도화 1·2 화면(로그인, 기록 업로드·전사 확인·삭제, 말투 프로필, 설정·계정 삭제, 대화 이력)과 SC-09 근거 기록·근거 없음 응답 스타일은 이번 이슈에서 구현하지 않습니다. 실제 백엔드 계약 및 해당 기능 작업에서 이어갑니다.

입력창은 키보드가 열리면 최대 두 줄로 줄이고 내부를 스크롤합니다. 텍스트 응답 생성 중에는 전송과 음성 전환을 막고, 실패한 메시지에는 다시 보내기 아이콘과 응답 자리의 재시도·수정 버튼을 제공합니다.

## 구조와 동작

- `lib/main.dart`: 앱 시작과 공통 테마
- `lib/home/home_screen.dart`: 홈과 새 대화 진입
- `lib/chat/ai_disclosure_sheet.dart`: AI 안내 및 이해 확인
- `lib/chat/voice_screen.dart`: 음성 화면 틀과 텍스트 전환
- `lib/shared/conversation_widgets.dart`: 공통 색상과 AI 배지
- `lib/config/app_config.dart`: 빌드 환경·백엔드 주소 파싱 및 검증
- `lib/chat/chat_service.dart`: 교체 가능한 응답 서비스, 현재 데모 구현
- `lib/chat/chat_screen.dart`: 대화 목록, 입력창, 로딩, 오류 및 재시도
- `test/`: 화면 흐름·키보드 영역·환경 설정 검사

입력은 최대 2,000자이며 공백만 있는 메시지는 전송할 수 없습니다. 응답 대기 중에는 중복 전송을 막습니다. 응답 실패·빈 응답·30초 타임아웃은 오류 안내로 처리하고, 같은 메시지 재시도 또는 메시지 수정을 제공합니다. 화면을 닫은 후 응답이 도착해도 상태를 갱신하지 않습니다.

오류 화면은 테스트에서 실패하는 서비스를 주입해 검증합니다. 현재 데모 서비스는 정상 응답만 반환하며 실제 네트워크 오류는 재현하지 않습니다.

## 직접 실행 및 검증

```powershell
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build web --dart-define-from-file=config/dev.local.json
```

이번 홈·안내·음성 화면 추가 이후에는 사용자 요청에 따라 정적 분석·테스트·앱 실행을 수행하지 않았습니다. 기존 테스트는 텍스트 화면을 직접 열도록 진입점을 맞췄으며, 위 명령으로 직접 실행할 수 있습니다. 실제 Android·iOS에서는 다음 항목도 직접 확인합니다.

- 입력창과 전송 버튼이 작은 화면·가로 화면·키보드 열린 상태에서 사용 가능한지
- 긴 입력과 긴 대화 목록에 넘침이 없는지
- 대화 전송 후 최신 메시지가 보이고 키보드를 닫아도 화면이 정상인지
- 기기의 한글 입력·선택·붙여넣기가 정상인지

자동 테스트는 실제 기기의 키보드 동작과 플랫폼 실행 검증을 대체하지 않습니다. PR에 기록할 현재 검증 상태는 `VALIDATION.md`를 참고하세요.
