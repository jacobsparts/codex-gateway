"""Unit tests for Codex SSE -> Responses object reconstruction (no network)."""

from __future__ import annotations

import copy
import json

from codex_transport import codex

def _events():
    """A real (trimmed) Codex SSE transcript: reasoning, text and one tool call."""
    return [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress", "output": []}},
        {"type": "response.in_progress", "response": {"id": "resp_1", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": "abc"},
        },
        {"type": "response.reasoning_summary_part.added", "output_index": 0, "summary_index": 0},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "Thinking"},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": " hard."},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": "abc"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []},
        },
        {"type": "response.content_part.added", "output_index": 1, "content_index": 0, "part": {"type": "output_text", "text": ""}},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "Hi"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": " there"},
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": ""}],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {"id": "fc_1", "type": "function_call", "name": "get_weather", "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"Paris"}'},
        {
            "type": "response.output_item.done",
            "output_index": 2,
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "name": "get_weather",
                "arguments": '{"city":"Paris"}',
                "call_id": "call_1",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                # Upstream always ships an empty output array.
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "attribution": {"items": {"msg_1": {"input_tokens": 1}}},
                },
            },
        },
    ]

def _final_response(events):
    builder = codex._ResponseBuilder()
    final = None
    for event in events:
        builder.add(event)
        if event.get("type") == "response.completed":
            final = copy.deepcopy(event["response"])
    if not final.get("output"):
        final["output"] = builder.output()
    usage = final.get("usage")
    if isinstance(usage, dict) and "attribution" in usage:
        final["usage"] = {k: v for k, v in usage.items() if k != "attribution"}
    return final

def test_reconstructs_reasoning_summary_from_deltas():
    response = _final_response(_events())
    reasoning = response["output"][0]
    assert reasoning["type"] == "reasoning"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "Thinking hard."}]

def test_reconstructs_text_from_deltas():
    response = _final_response(_events())
    message = response["output"][1]
    assert message["content"][0]["text"] == "Hi there"


def test_normalizes_codex_final_answer_phase():
    event = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Hi"}],
        },
    }
    normalized = codex._normalize_event(event)
    assert normalized["item"]["phase"] == "final"
    assert event["item"]["phase"] == "final_answer"

def test_reconstructs_function_call_arguments():
    response = _final_response(_events())
    call = response["output"][2]
    assert call["call_id"] == "call_1"
    assert json.loads(call["arguments"]) == {"city": "Paris"}

def test_output_order_and_ids():
    response = _final_response(_events())
    assert [item["id"] for item in response["output"]] == ["rs_1", "msg_1", "fc_1"]

def test_usage_drops_attribution_blob():
    response = _final_response(_events())
    assert response["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

def test_empty_stream_yields_empty_output():
    response = _final_response(
        [{"type": "response.completed", "response": {"id": "r", "status": "completed", "output": []}}]
    )
    assert response["output"] == []

def test_prepare_body_forces_streaming_and_unstored():
    body = codex.prepare_body({"model": "gpt-5.6-luna", "input": [{"role": "user", "content": "hi"}]})
    assert body["stream"] is True
    assert body["store"] is False
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert body["instructions"] == ""

def test_prepare_body_drops_unsupported_params():
    body = codex.prepare_body(
        {
            "model": "gpt-5.6-luna",
            "input": [],
            "temperature": 0.7,
            "top_p": 0.9,
            "max_output_tokens": 100,
            "truncation": "auto",
            "metadata": {"a": "b"},
            "previous_response_id": "resp_x",
            "presence_penalty": 0.5,
            "safety_identifier": "s",
        }
    )
    for key in (
        "temperature",
        "top_p",
        "max_output_tokens",
        "truncation",
        "metadata",
        "previous_response_id",
        "presence_penalty",
        "safety_identifier",
    ):
        assert key not in body, key

def test_prepare_body_keeps_supported_params():
    body = codex.prepare_body(
        {
            "model": "gpt-5.6-luna",
            "input": [],
            "instructions": "be terse",
            "tools": [{"type": "function", "name": "f"}],
            "tool_choice": {"type": "function", "name": "f"},
            "parallel_tool_calls": False,
            "reasoning": {"effort": "xhigh"},
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
        }
    )
    assert body["instructions"] == "be terse"
    assert body["tools"] == [{"type": "function", "name": "f"}]
    assert body["tool_choice"] == {"type": "function", "name": "f"}
    assert body["parallel_tool_calls"] is False
    assert body["reasoning"] == {"effort": "xhigh"}
    assert body["text"] == {"verbosity": "low"}

def test_quota_snapshot_from_headers():
    headers = {
        "x-codex-primary-used-percent": "42.5",
        "x-codex-primary-reset-at": "1788553569",
        "x-codex-secondary-used-percent": "67",
        "x-codex-secondary-reset-at": "1788816640",
        "content-type": "text/event-stream",
    }
    snapshot = codex.quota_snapshot(headers)
    assert snapshot["limits"]["codex_primary"]["used_percent"] == 42.5
    assert snapshot["limits"]["codex_primary"]["reset_at"] == 1788553569
    assert snapshot["limits"]["codex_secondary"]["used_percent"] == 67

def test_rate_limits_passthrough_only_codex_headers():
    headers = {"X-Codex-Primary-Used-Percent": "10", "Content-Type": "text/event-stream"}
    assert codex._rate_limits(headers) == {"x-codex-primary-used-percent": "10"}

def test_credential_selection_prefers_least_used():
    auth = codex.CodexAuth.__new__(codex.CodexAuth)
    auth.credentials = [
        {"email": "a", "rate_limits": {"limits": {"codex_primary": {"used_percent": 95.0}, "codex_secondary": {"used_percent": 67.0}}}},
        {"email": "b", "rate_limits": {"limits": {"codex_primary": {"used_percent": 1.0}, "codex_secondary": {"used_percent": 67.0}}}},
        {"email": "c", "invalid": "2026-01-01"},
    ]
    assert auth.select_credential()["email"] == "b"

def test_credential_selection_skips_invalid():
    auth = codex.CodexAuth.__new__(codex.CodexAuth)
    auth.credentials = [
        {"email": "a", "invalid": "2026-01-01"},
        {"email": "b", "rate_limits": {"limits": {"codex_primary": {"used_percent": 3.0}}}},
    ]
    assert auth.select_credential()["email"] == "b"

def test_jwt_payload_decodes_claims():
    import base64

    def segment(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return raw

    token = f"header.{segment({'exp': 1893456000, 'email': 'x@y.z'})}.sig"
    assert codex.jwt_payload(token)["email"] == "x@y.z"
    assert codex.jwt_exp(token).year == 2030
    assert codex.jwt_exp("not-a-jwt") is None
