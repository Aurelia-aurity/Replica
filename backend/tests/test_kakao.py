"""카카오톡 내보내기 분석: 앱(`text_analysis.dart`)과 기본 규칙은 같고, `]` 이름·머리글·상한은 서버만 처리한다."""

import pytest

from app.services.kakao import (
    MAX_SPEAKER_NAME, MAX_SPEAKERS, PLAIN, BlankText, NoChatMessages, SpeakerNameTooLong, TooManySpeakers,
    analyze_text)

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


def test_a_name_of_exactly_the_limit_is_a_speaker():
    name = "나" * MAX_SPEAKER_NAME
    result = analyze_text(_lines([name, name]))
    assert result.kakao and result.speakers == [name]


def test_a_speaker_name_over_the_limit_is_rejected_not_skipped():
    long_name, ok_name = "가" * (MAX_SPEAKER_NAME + 1), "나" * MAX_SPEAKER_NAME
    # 긴 이름만 있는 파일, 정상 화자와 섞인 파일, 이름이 한 번만 나오는 파일 모두 일반 텍스트로 바뀌지 않고 거절된다.
    for names in ([long_name, long_name], [ok_name, ok_name, long_name], [ok_name, ok_name, ok_name, long_name]):
        with pytest.raises(SpeakerNameTooLong):
            analyze_text(_lines(names))
    dated = f"2025. 9. 20. 오후 3:12, {long_name} : 안녕\n2025. 9. 20. 오후 3:13, 나 : 응\n".encode()
    with pytest.raises(SpeakerNameTooLong):  # 날짜형 내보내기도 같다
        analyze_text(dated)


def test_a_speaker_name_may_contain_closing_brackets():
    data = ("[엄마 [회사]] [오후 3:12] 안녕\n[나] [오후 3:13] 응\n[엄마 [회사]] [오후 3:14] 밥 먹었니\n"
            "[a [b] [c]] [오전 9:00] x\n").encode()
    result = analyze_text(data)
    assert result.kakao and result.messages == 4
    assert result.speakers == ["엄마 [회사]", "나", "a [b] [c]"]  # 메시지가 많은 순, 같으면 먼저 나온 순


def test_the_name_ends_at_the_first_time_boundary():
    # 본문에 `] [오후 4:00] ` 같은 문자열이 들어 있어도 이름은 첫 시각 경계까지다.
    result = analyze_text("[엄마] [오후 3:12] 인용 [나] [오후 4:00] 말\n[나] [오후 3:13] 응\n".encode())
    assert result.speakers == ["엄마", "나"] and result.messages == 2


def test_the_number_of_distinct_speakers_is_capped():
    at_limit = analyze_text(_lines(f"u{n}" for n in range(MAX_SPEAKERS)))
    assert at_limit.kakao and len(at_limit.speakers) == MAX_SPEAKERS
    repeated = analyze_text(_lines([f"u{n}" for n in range(MAX_SPEAKERS)] + ["u0", "u1"]))  # 이미 센 이름은 한도와 무관
    assert len(repeated.speakers) == MAX_SPEAKERS
    with pytest.raises(TooManySpeakers):
        analyze_text(_lines(f"u{n}" for n in range(MAX_SPEAKERS + 1)))


PC_HEADER = "엄마 님과 카카오톡 대화\n저장한 날짜 : 2026년 11월 23일 오후 6:19\n\n"
ANDROID_HEADER = "엄마 님과 카카오톡 대화\n저장한 날짜 : 2025-09-20 15:00:00\n\n--------------- 2025년 9월 20일 토요일 ---------------\n"


def test_a_pc_export_with_its_header_and_a_date_line_is_recognized():
    data = (PC_HEADER + "2023년 2월 28일 오후 7:51\n2023년 2월 28일 오후 7:51, 엄마 : 안녕\n"
            "2023년 2월 28일 오후 7:52, 나 : 응\n2023년 2월 28일 오후 7:53, 엄마 : 밥 먹었니\n").encode()
    result = analyze_text(data)
    assert result.kakao and result.speakers == ["엄마", "나"] and result.messages == 3


@pytest.mark.parametrize("text", [PC_HEADER + "2023년 2월 28일 오후 7:51, 엄마 : 안녕\n",
                                  ANDROID_HEADER + "[엄마] [오후 3:12] 안녕\n"])
def test_a_single_message_export_with_its_header_is_a_chat(text):
    result = analyze_text(text.encode())
    assert result.kakao and result.speakers == ["엄마"] and result.messages == 1


def test_a_header_without_any_message_is_rejected():
    with pytest.raises(NoChatMessages):
        analyze_text((PC_HEADER + "2023년 2월 28일 오후 7:51\n").encode())


def test_a_single_message_without_the_full_header_is_still_plain_text():
    assert analyze_text("[엄마] [오후 3:12] 안녕\n".encode()) == PLAIN
    assert analyze_text(("엄마 님과 카카오톡 대화\n" + "2023년 2월 28일 오후 7:51, 엄마 : 안녕\n").encode()) == PLAIN  # 저장한 날짜 줄이 없음
    assert analyze_text(("일기\n저장한 날짜 : 오늘\n2023년 2월 28일 오후 7:51, 엄마 : 안녕\n").encode()) == PLAIN  # 제목 줄이 없음
    late = ("\n" * 5 + PC_HEADER + "2023년 2월 28일 오후 7:51, 엄마 : 안녕\n").encode()  # 앞쪽 5줄 밖의 머리글은 보지 않음
    assert analyze_text(late) == PLAIN


@pytest.mark.parametrize("data", [
    b"", b"\xef\xbb\xbf", b"\xef\xbb\xbf   \n", "\u3000\u3000".encode(), "\u00a0".encode(), b" \t\r\n",
])
def test_text_that_is_blank_after_decoding_is_refused_not_treated_as_plain(data):
    with pytest.raises(BlankText):
        analyze_text(data)


def test_text_with_any_visible_character_is_not_blank():
    assert analyze_text(b"\xef\xbb\xbf\n  x  \n") is PLAIN


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
