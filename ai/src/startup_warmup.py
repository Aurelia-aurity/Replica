"""Fixed synthetic startup requests, independent of HTTP admission."""
from inference import Request
import persona_contract


def synthetic_id(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def persona_fixture(evidence_count, request_id=None):
    """Shared synthetic bytes source for startup and the unfrozen local harness."""
    return {
        "request_id": request_id or synthetic_id(4),
        "persona_id": synthetic_id(1),
        "persona": {"display_name": "Synthetic Rowan", "provider_address": None},
        "source_revision": 1,
        "message": "What is my synthetic activity? Reply in one short sentence.",
        "history": [], "max_new_tokens": 160,
        "style_profile": {
            "id": synthetic_id(2), "version": 1, "source_revision": 1,
            "source_snapshot_id": synthetic_id(3),
            "content": {
                "schema_version": 1,
                "speech_style": {"tone": [], "address_terms": [], "sentence_endings": [],
                                 "frequent_expressions": []},
                "conversation_habits": [], "limitations": [],
            },
        },
        "evidence": [
            {"chunk_id": synthetic_id(100 + i), "record_id": synthetic_id(200 + i),
             "text_version_id": synthetic_id(300 + i), "speaker_role": "persona",
             "content": f"Synthetic record {i:02d}: I enjoy walking by the river."}
            for i in range(1, evidence_count + 1)
        ],
    }


def warmup(engine, persona_generator):
    """Exactly three validated calls; no slot, user request, retries or text logs."""
    for count in (0, 3, 10):
        if persona_generator is None:
            request = Request(synthetic_id(4), "Reply with one short greeting.", (), 160)
            selected = engine
        else:
            request = persona_contract.parse_request(persona_fixture(count))
            selected = persona_generator
        prepared = selected.prepare(request)
        result = selected.generate(request, prepared)
        if persona_generator is not None:
            persona_contract.success_body(request, prepared, result)
        elif (not result.message.strip() or result.finish_reason not in ("stop", "length")
              or type(result.output_tokens) is not int or result.output_tokens < 0):
            raise ValueError
