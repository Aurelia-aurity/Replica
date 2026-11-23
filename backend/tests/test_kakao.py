"""카카오톡 내보내기 분석: 앱(`text_analysis.dart`)과 같은 판정 규칙."""

from app.services.kakao import PLAIN, analyze_text

ANDROID = "[엄마] [오후 3:12] 밥 먹었니\n[나] [오후 3:13] 응\n[엄마] [오후 3:14] 다행이다\n".encode()
DATED = (
    "2025. 9. 20. 오후 3:12, 엄마 : 밥 먹었니\n"
    "2025. 9. 20. 오후 3:13, 나 : 응\n"
    "2025년 9월 21일 일요일 오전 8:05, 엄마 : 좋은 아침\n"
).encode()


def test_android_export_counts_speakers_by_message_count():
    result = analyze_text(ANDROID)
    assert result.kakao and result.speakers == ["엄마", "나"] and result.messages == 3


def test_dated_export_variants_are_recognized():
    result = analyze_text(DATED)
    assert result.kakao and result.speakers == ["엄마", "나"] and result.messages == 3


def test_bom_and_malformed_bytes_do_not_break_analysis():
    data = b"\xef\xbb\xbf" + ANDROID + b"\xff\xfe broken line\n"
    assert analyze_text(data).speakers == ["엄마", "나"]


def test_a_single_stray_timestamp_is_not_a_chat_export():
    assert analyze_text("[엄마] [오후 3:12] 안녕\n그냥 일기 내용\n".encode()) == PLAIN


def test_plain_text_is_not_kakao():
    result = analyze_text("오늘은 날씨가 좋았다.\n산책을 했다.\n".encode())
    assert not result.kakao and result.speakers == []


def test_one_speaker_export_is_still_kakao():
    result = analyze_text("[엄마] [오후 3:12] 안녕\n[엄마] [오후 3:13] 잘 지내니\n".encode())
    assert result.kakao and result.speakers == ["엄마"]


def test_speaker_names_are_trimmed_and_blank_names_ignored():
    data = "[ 엄마 ] [오후 3:12] a\n[ 엄마 ] [오후 3:13] b\n".encode()
    assert analyze_text(data).speakers == ["엄마"]


def test_only_newline_cr_and_crlf_split_lines_like_the_app():
    data = "[엄마] [오후 3:12] a\r[나] [오후 3:13] b\r\n[엄마] [오후 3:14] c\n".encode()
    assert analyze_text(data).messages == 3
    # 줄 구분 문자(U+2028)나 수직 탭은 줄을 나누지 않는다(앱의 LineSplitter와 같음).
    data = "[엄마] [오후 3:12] a\u2028[나] [오후 3:13] b\n[엄마] [오후 3:14] c\n".encode()
    assert analyze_text(data).messages == 2
