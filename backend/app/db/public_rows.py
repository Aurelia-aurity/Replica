"""Allowlisted app-facing projections from the app-backend draft; never return raw DB rows."""
from typing import Any, Mapping

from .errors import ValidationError

_FIELDS = {
    "persona": "id name status data_revision created_at updated_at",
    "session": "id persona_id title ai_notice_ack_at created_at updated_at",
    "turn": "turn_no input_mode question question_status answer answer_status tts_status model_name "
            "stt_ms llm_ms tts_ms total_ms error_message created_at finish_reason",
    "record": "id persona_id kind original_name mime_type size_bytes status consent_at consent_version "
              "consent_scopes error_code created_at updated_at",
    "text_version": "id record_id revision content status confirmed_at created_at updated_at",
    "style_profile": "id persona_id version source_revision content model_name status created_at updated_at",
    "deletion_job": "id target_type target_id status remaining_steps attempt_count last_error_code "
                    "created_at updated_at finished_at",
}


def to_public_row(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit field allowlist, not a substitute for authentication/ownership checks."""
    if not isinstance(kind, str) or kind not in _FIELDS or not isinstance(row, Mapping):
        raise ValidationError()
    result = {name: row[name] for name in _FIELDS[kind].split() if name in row}
    if kind == "turn":
        result["turn_id"] = row.get("id")
        result["is_ai_generated"] = bool(row.get("answer"))
    return result
