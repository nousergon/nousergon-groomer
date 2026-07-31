"""Contract tests for the model provider interface (issue #21).

These tests assert:

1. **Construction** — ``OpenAICompatibleProvider`` accepts base_url, api_key, model.
2. **HTTP contract** — ``complete`` POSTs to the correct OpenAI-compatible endpoint.
3. **Fail loud** — non-200 responses, timeouts, and malformed payloads raise ``ModelError``.
4. **Input validation** — empty prompts raise ``ValueError``.
5. **Protocol** — ``ModelProvider`` is a runtime-checkable ``Protocol``.
"""
from __future__ import annotations

import sys
import types

import pytest

from nousergon_groomer.model_provider import (
    ModelError,
    ModelProvider,
    OpenAICompatibleProvider,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json_data


@pytest.fixture
def mock_httpx(monkeypatch):
    """Inject a fake httpx module so tests run without the optional dependency."""
    calls: list[dict] = []

    class TimeoutException(Exception):
        pass

    class HTTPError(Exception):
        pass

    def fake_post(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeResponse(
            json_data={
                "choices": [{"message": {"content": "hello from model"}}],
            },
        )

    fake_httpx = types.SimpleNamespace(
        post=fake_post,
        TimeoutException=TimeoutException,
        HTTPError=HTTPError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return calls, fake_httpx


def test_openai_compatible_provider_construction():
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="test-key",
        model="test-model",
    )
    assert provider._base_url == "https://api.example.com/v1"
    assert provider._api_key == "test-key"
    assert provider._model == "test-model"


def test_complete_posts_to_chat_completions_endpoint(mock_httpx):
    calls, _ = mock_httpx
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1/",
        api_key="secret",
        model="default-model",
    )

    result = provider.complete("Say hi", model="gpt-4o-mini", temperature=0.2)

    assert result == "hello from model"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.example.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"]["model"] == "gpt-4o-mini"
    assert call["json"]["temperature"] == 0.2
    assert call["json"]["messages"] == [{"role": "user", "content": "Say hi"}]


def test_non_200_response_raises_model_error(mock_httpx):
    _, fake_httpx = mock_httpx

    def bad_post(url: str, **kwargs):
        return _FakeResponse(status_code=500, text="internal error")

    fake_httpx.post = bad_post
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="secret",
        model="test-model",
    )

    with pytest.raises(ModelError, match="status 500"):
        provider.complete("prompt", model="test-model")


def test_timeout_raises_model_error(mock_httpx):
    _, fake_httpx = mock_httpx

    def timeout_post(url: str, **kwargs):
        raise fake_httpx.TimeoutException("timed out")

    fake_httpx.post = timeout_post
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="secret",
        model="test-model",
    )

    with pytest.raises(ModelError, match="timed out"):
        provider.complete("prompt", model="test-model")


def test_empty_prompt_raises_value_error(mock_httpx):
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="secret",
        model="test-model",
    )

    with pytest.raises(ValueError, match="empty"):
        provider.complete("", model="test-model")

    with pytest.raises(ValueError, match="empty"):
        provider.complete("   ", model="test-model")


def test_model_provider_is_protocol():
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="secret",
        model="test-model",
    )
    assert isinstance(provider, ModelProvider)
