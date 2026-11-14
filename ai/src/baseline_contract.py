"""Baseline HTTP request and fixed error envelopes."""
import re

from inference import Request

RAW_LIMIT = 262144
ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ERRORS = {
    400: ("INVALID_REQUEST", "요청 형식이 올바르지 않습니다."),
    401: ("SERVICE_UNAUTHENTICATED", "서비스 인증이 필요합니다."),
    403: ("SERVICE_FORBIDDEN", "서비스 호출 권한이 없습니다."),
    413: ("PAYLOAD_TOO_LARGE", "요청 크기가 한도를 초과했습니다."),
    422: ("CONTEXT_LIMIT_EXCEEDED", "질문이 모델 문맥 한도를 초과했습니다."),
    503: ("MODEL_NOT_READY", "모델이 아직 준비되지 않았습니다."),
    409: ("REQUEST_IN_PROGRESS", "동일 요청이 처리 중입니다."),
    429: ("AI_BUSY", "AI가 다른 요청을 처리 중입니다."),
    504: ("INFERENCE_DEADLINE_EXCEEDED", "추론 마감 시간을 초과했습니다."),
    500: ("INFERENCE_FAILED", "답변 생성에 실패했습니다."),
}


def error_body(status: int, request_id: str | None = None):
    code, message = ERRORS[status]
    return {"request_id": request_id, "error": {"code": code, "message": message}}


def valid_id(value) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def reject_json_constant(_value: str) -> None:
    raise ValueError


def parse_request(data) -> Request:
    """Check all structural rules before checking any parsed length limit."""
    if not isinstance(data, dict) or set(data) != {
            "request_id", "message", "history", "max_new_tokens"}:
        raise ValueError
    if not valid_id(data["request_id"]):
        raise ValueError
    if not isinstance(data["message"], str) or not data["message"].strip():
        raise ValueError
    if not isinstance(data["history"], list):
        raise ValueError
    budget = data["max_new_tokens"]
    # JSON Schema integers include exactly integral JSON numbers, but not booleans.
    if type(budget) not in (int, float) or not 1 <= budget <= 1024 or int(budget) != budget:
        raise ValueError
    pairs = []
    for pair in data["history"]:
        if not isinstance(pair, dict) or set(pair) != {"user", "assistant"}:
            raise ValueError
        if any(not isinstance(pair[key], str) or not pair[key].strip()
               for key in ("user", "assistant")):
            raise ValueError
        pairs.append((pair["user"], pair["assistant"]))
    return Request(data["request_id"], data["message"], tuple(pairs), int(budget))
