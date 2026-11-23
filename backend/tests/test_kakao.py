"""카카오톡 내보내기 분석: 앱(`text_analysis.dart`)과 같은 판정 규칙."""

import pytest

from app.services.kakao import MAX_SPEAKER_NAME, MAX_SPEAKERS, PLAIN, TooManySpeakers, analyze_text

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


def test_a_leading_bom_is_ignored():
    assert analyze_text(b"\xef\xbb\xbf" + ANDROID).speakers == ["엄마", "나"]


def test_bytes_that_are_not_utf8_raise_instead_of_garbling_speaker_names():
    with pytest.raises(UnicodeDecodeError):
        analyze_text(ANDROID + b"\xff\xfe broken line\n")


def _lines(names):
    return "".join(f"[{name}] [오후 3:12] 안녕\n" for name in names).encode()


def test_a_speaker_name_over_the_limit_is_not_counted_as_a_speaker():
    long_name, ok_name = "가" * (MAX_SPEAKER_NAME + 1), "나" * MAX_SPEAKER_NAME
    result = analyze_text(_lines([ok_name, ok_name, long_name, long_name, long_name]))
    assert result.kakao and result.speakers == [ok_name] and result.messages == 2  # 긴 이름의 줄은 메시지로 세지 않음
    assert analyze_text(_lines([long_name, long_name])) == PLAIN


def test_the_number_of_distinct_speakers_is_capped():
    at_limit = analyze_text(_lines(f"u{n}" for n in range(MAX_SPEAKERS)))
    assert at_limit.kakao and len(at_limit.speakers) == MAX_SPEAKERS
    repeated = analyze_text(_lines([f"u{n}" for n in range(MAX_SPEAKERS)] + ["u0", "u1"]))  # 이미 센 이름은 한도와 무관
    assert len(repeated.speakers) == MAX_SPEAKERS
    with pytest.raises(TooManySpeakers):
        analyze_text(_lines(f"u{n}" for n in range(MAX_SPEAKERS + 1)))


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
