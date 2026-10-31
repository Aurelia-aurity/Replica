# 기록 화면 미리보기

SC-03·SC-04 기획서를 참고해 390×844 논리 크기에서 Flutter 위젯을 렌더링한 이미지입니다. 실제 기기 캡처가 아닙니다. 합성 파일명·시간·길이·상태를 사용하며 실제 자료 업로드나 개인화 처리를 검증한 화면이 아닙니다.

- `record-list.png`: 전체·유형 필터, 행 오른쪽의 상태 배지와 실패 항목, 확정 자료 안내. 재시도 API가 없는 서비스에서는 버튼을 비활성화합니다.
- `record-upload.png`: 기록 종류 카드, 파일 카드·변경, 화자 선택 준비 안내, 동의 카드, 하단 고정 올리기와 취소.
- `transcript-review.png`: SC-05 전사문 전체 수정, 원본 재생 미연결 안내, 확정 안내와 하단 버튼. API에 구간 시간이 없어 시간 표시는 만들지 않습니다.
- `record-detail.png`: SC-06을 참고한 확정 기록 상세와 삭제 진입 버튼.
- `record-delete-confirm.png`, `record-delete-partial-failure.png`: 이슈 #11의 삭제 확인 시트와 남은 검색 색인 표시. 모의 서비스가 반환한 상태이며 실제 파일 삭제 결과가 아닙니다.

화자 목록·메시지 수를 추출하는 기능은 아직 없으며, 기획서의 예시 이름·숫자를 실제 분석 결과처럼 표시하지 않습니다. `ready`만으로 개인화 반영을 증명할 수 없어 실제 API 데이터에는 계속 ‘확인 필요’를 표시합니다. 캡처의 ‘반영됨’은 명시적으로 주입한 합성 상태입니다.

Windows에서 캡처 재생성: `flutter test --no-pub test/records_design_test.dart --dart-define=RECORD_UI_CAPTURES=true`. 캡처 모드는 Windows 맑은 고딕과 `C:/flutter`의 Material Icons를 로드합니다. 일반 테스트는 해당 파일에 의존하지 않습니다.
