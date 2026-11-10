"""Unit tests for the AI grading contract, adapters, and registry."""

from typing import Any

import pytest
from app.core.errors import ExternalServiceError
from app.services.ai.batching import (
    BatchGradeOutcome,
    GradingProviderBundle,
    resolve_grading_provider,
)
from app.services.ai.llm import parse_batch_entry
from app.services.ai.openai import parse_output_line, strict_schema
from app.services.ai.schema import (
    GradingResult,
    grading_response_schema,
    letter_for,
    parse_free_text_text,
    parse_grading_text,
)
from pydantic import ValidationError


class TestGradingResult:
    async def test_accepts_valid_payload(self) -> None:
        result = GradingResult(score=4.0, summary="s", feedback="f")
        assert result.score == 4.0
        assert result.plagiarism_score is None

    async def test_accepts_camel_case_aliases(self) -> None:
        result = GradingResult.model_validate(
            {
                "score": 3.5,
                "summary": "s",
                "feedback": "f",
                "plagiarismScore": 88.5,
                "plagiarismAnalysis": "overlap found",
            }
        )
        assert result.plagiarism_score == 88.5
        assert result.plagiarism_analysis == "overlap found"

    async def test_rejects_missing_score(self) -> None:
        with pytest.raises(ValidationError):
            GradingResult.model_validate({"summary": "s", "feedback": "f"})

    async def test_rejects_negative_score(self) -> None:
        with pytest.raises(ValidationError):
            GradingResult(score=-1.0, summary="s", feedback="f")

    async def test_rejects_plagiarism_above_100(self) -> None:
        with pytest.raises(ValidationError):
            GradingResult(score=1.0, summary="s", feedback="f", plagiarism_score=120.0)

    async def test_ignores_extra_fields(self) -> None:
        result = GradingResult.model_validate(
            {"score": 2.0, "summary": "s", "feedback": "f", "surprise": True}
        )
        assert result.score == 2.0


class TestParseGradingText:
    async def test_parses_plain_json(self) -> None:
        result = parse_grading_text('{"score": 3, "summary": "s", "feedback": "f"}')
        assert result.score == 3.0

    async def test_strips_markdown_fences(self) -> None:
        text = '```json\n{"score": 3, "summary": "s", "feedback": "f"}\n```'
        assert parse_grading_text(text).score == 3.0

    async def test_invalid_json_raises(self) -> None:
        with pytest.raises(ExternalServiceError):
            parse_grading_text("not json at all")

    async def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ExternalServiceError):
            parse_grading_text('{"score": 3}')

    async def test_free_text_parse(self) -> None:
        result = parse_free_text_text('{"score": 2.5, "summary": "s", "feedback": "f"}')
        assert result.score == 2.5


class TestLetterFor:
    async def test_exact_anchors(self) -> None:
        assert letter_for(4.0) == "A"
        assert letter_for(3.0) == "B"
        assert letter_for(0.0) == "F"
        assert letter_for(0.5) == "E"

    async def test_snaps_to_nearest_anchor(self) -> None:
        assert letter_for(2.2) == "C"
        assert letter_for(3.9) == "A"


class TestResponseSchema:
    async def test_grading_schema_has_numeric_score(self) -> None:
        schema = grading_response_schema()
        assert schema["properties"]["score"]["type"] == "number"
        assert set(schema["required"]) == {"score", "summary", "feedback"}

    async def test_nullable_fields_use_type_arrays(self) -> None:
        schema = grading_response_schema()
        score_prop = schema["properties"]["plagiarismScore"]
        assert score_prop.get("anyOf") or score_prop.get("type") == ["number", "null"]

    async def test_validation_accepts_snake_case_too(self) -> None:
        result = GradingResult.model_validate(
            {"score": 1.0, "summary": "s", "feedback": "f", "plagiarism_score": 40.0}
        )
        assert result.plagiarism_score == 40.0


class TestGeminiParseBatchEntry:
    def _valid_text(self) -> str:
        return '{"score": 3, "summary": "s", "feedback": "f"}'

    def _entry(self, text: str | None = None, **overrides: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "key": "req-1",
            "response": {
                "candidates": [{"content": {"parts": [{"text": text or self._valid_text()}]}}]
            },
        }
        entry.update(overrides)
        return entry

    async def test_valid_entry_parses(self) -> None:
        outcome = parse_batch_entry(self._entry())
        assert outcome.key == "req-1"
        assert outcome.result is not None
        assert outcome.result.score == 3.0
        assert outcome.error is None

    async def test_error_entry_becomes_failure(self) -> None:
        outcome = parse_batch_entry({"key": "req-1", "error": {"message": "quota"}})
        assert outcome.result is None
        assert "quota" in (outcome.error or "")

    async def test_missing_candidates_becomes_failure(self) -> None:
        outcome = parse_batch_entry({"key": "req-1", "response": {}})
        assert outcome.result is None
        assert "candidates" in (outcome.error or "")

    async def test_malformed_text_becomes_failure(self) -> None:
        outcome = parse_batch_entry(self._entry(text="truncated json {"))
        assert outcome.result is None
        assert outcome.error is not None

    async def test_missing_key_empty(self) -> None:
        outcome = parse_batch_entry({"response": {"candidates": []}})
        assert outcome.key == ""


class TestOpenAIStrictSchema:
    async def test_all_properties_required(self) -> None:
        schema = strict_schema(grading_response_schema())
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])

    async def test_nullable_property_uses_type_array(self) -> None:
        schema = strict_schema(grading_response_schema())
        prop = schema["properties"]["plagiarismScore"]
        assert "null" in prop["type"]
        assert "anyOf" not in prop

    async def test_unsupported_keywords_stripped(self) -> None:
        schema = strict_schema(
            {"title": "X", "properties": {"a": {"type": "string", "minimum": 0}}}
        )
        assert "title" not in schema
        assert "minimum" not in schema["properties"]["a"]


class TestOpenAIParseOutputLine:
    async def test_valid_line_parses(self) -> None:
        line = {
            "custom_id": "req-1",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [
                        {"message": {"content": '{"score": 4, "summary": "s", "feedback": "f"}'}}
                    ]
                },
            },
        }
        outcome = parse_output_line(line)
        assert outcome.result is not None
        assert outcome.result.score == 4.0

    async def test_error_line_becomes_failure(self) -> None:
        outcome = parse_output_line({"custom_id": "req-1", "error": {"message": "boom"}})
        assert outcome.result is None

    async def test_non_200_becomes_failure(self) -> None:
        line = {"custom_id": "req-1", "response": {"status_code": 429}}
        outcome = parse_output_line(line)
        assert outcome.result is None
        assert "429" in (outcome.error or "")

    async def test_empty_content_becomes_failure(self) -> None:
        line = {
            "custom_id": "req-1",
            "response": {"status_code": 200, "body": {"choices": [{"message": {}}]}},
        }
        outcome = parse_output_line(line)
        assert outcome.result is None


class TestResolveGradingProvider:
    async def test_empty_registry_returns_none(self) -> None:
        assert resolve_grading_provider({}, "gemini") is None

    async def test_named_provider_wins(self) -> None:
        gemini = GradingProviderBundle()
        openai = GradingProviderBundle()
        registry = {"gemini": gemini, "openai": openai}
        assert resolve_grading_provider(registry, "openai") is openai

    async def test_unknown_name_falls_back_to_first(self) -> None:
        gemini = GradingProviderBundle()
        registry = {"gemini": gemini}
        assert resolve_grading_provider(registry, "anthropic") is gemini


class TestBatchGradeOutcome:
    async def test_failed_factory_sets_error_only(self) -> None:
        outcome = BatchGradeOutcome.failed("k", "bad json")
        assert outcome.key == "k"
        assert outcome.result is None
        assert outcome.error == "bad json"
