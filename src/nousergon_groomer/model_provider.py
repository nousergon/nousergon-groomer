"""Model provider interface for the groomer's leaf LLM calls (v0.2.0).

Provider-agnostic protocol with an OpenAI-compatible default implementation.
Uses direct HTTP to ``/v1/chat/completions`` endpoints shared by xAI, Moonshot,
Zhipu, DeepSeek, and other non-Anthropic providers.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ModelError", "ModelProvider", "OpenAICompatibleProvider"]


class ModelError(Exception):
    """Raised when a model provider API call fails."""


@runtime_checkable
class ModelProvider(Protocol):
    def complete(self, prompt: str, *, model: str, temperature: float = 0.0) -> str:
        """Generate a completion via the provider's API."""
        ...


class OpenAICompatibleProvider:
    """Default model provider using OpenAI-compatible ``/v1/chat/completions``."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def complete(self, prompt: str, *, model: str, temperature: float = 0.0) -> str:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")

        try:
            import httpx
        except ImportError as exc:
            raise ModelError(
                "httpx is required for OpenAICompatibleProvider; "
                "install with pip install nousergon-groomer[model]"
            ) from exc

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }

        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=60.0)
        except httpx.TimeoutException as exc:
            raise ModelError(f"request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"HTTP error: {exc}") from exc

        if response.status_code != 200:
            raise ModelError(
                f"API returned status {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError(f"malformed API response: {exc}") from exc

        if not isinstance(content, str):
            raise ModelError("malformed API response: content is not a string")

        return content
