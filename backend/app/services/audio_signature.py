"""기록 음성 파일의 머리 시그니처 확인 (contracts/app-backend/api-spec.md '업로드 뒤 검증'의 '내용 형식').

예약한 `mime_type`과 파일의 첫 바이트가 맞는지만 본다. 디코딩까지는 하지 않는다(전사 단계에서 공급자가 알려 준다).
"""

HEAD_BYTES = 16  # 아래 규칙이 보는 최대 길이

_FTYP = ("audio/mp4", "audio/x-m4a")
_WAV = ("audio/wav", "audio/x-wav")


def signature_matches(mime_type: str, head: bytes) -> bool:
    if mime_type in _WAV:
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if mime_type == "audio/mpeg":  # ID3 태그 또는 MPEG 프레임 동기
        return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if mime_type in _FTYP:
        return head[4:8] == b"ftyp"
    return False
