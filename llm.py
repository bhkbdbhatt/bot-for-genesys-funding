"""Optional external renderer.

The bot never calls an LLM API by default. You may supply either:

1. A local CLI subprocess (no API key, no network to third parties) by setting
   `scheduler.llm_command`. The command receives the rendered prompt on stdin
   and must print the final message on stdout.
2. An HTTP API endpoint (e.g. OpenAI-compatible, Anthropic, Ollama) by setting
   `scheduler.llm_api.*`. The API key is read from the environment variable
   named by `api_key_env` (never persisted to disk). This is entirely optional
   and falls back to the deterministic composer on any error.

Both modes are fail-soft and the deterministic composer remains the default.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from logging_setup import get_logger

LOG = get_logger("llm")

MIN_OUTPUT_CHARS = 40


class LLMError(Exception):
    """Raised when the configured renderer is unusable or fails."""


class SubprocessLLM:
    """Callable renderer backed by a local command."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: int = 120,
        min_chars: int = MIN_OUTPUT_CHARS,
    ) -> None:
        self.command: List[str] = [str(part) for part in command if str(part).strip()]
        self.timeout = max(int(timeout), 5)
        self.min_chars = max(int(min_chars), 1)
        if not self.command:
            raise LLMError("llm_command is empty; set scheduler.llm_command to enable --llm")
        self.kind = "subprocess"

    # -- introspection ----------------------------------------------------- #

    @property
    def executable(self) -> str:
        return self.command[0] if self.command else ""

    def exists(self) -> bool:
        return bool(self.command) and shutil.which(self.command[0]) is not None

    def describe(self) -> str:
        return " ".join(self.command)

    # -- rendering --------------------------------------------------------- #

    def __call__(self, prompt: str) -> str:
        if not self.command:
            raise LLMError("no renderer command configured")
        if not self.exists():
            raise LLMError(
                f"renderer executable {self.command[0]!r} not found on PATH "
                f"(command: {self.describe()})"
            )
        LOG.info("Calling renderer (subprocess): %s", self.describe())
        try:
            completed = subprocess.run(
                self.command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"renderer timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise LLMError(f"renderer could not be started: {exc}") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:400]
            raise LLMError(f"renderer exited {completed.returncode}: {stderr}")

        output = (completed.stdout or "").strip()
        if len(output) < self.min_chars:
            raise LLMError(f"renderer output too short ({len(output)} chars) - treating as failure")
        return output


def build_renderer(command: Sequence[str], timeout: int = 120) -> Optional[SubprocessLLM]:
    """Return a renderer, or None when no command is configured."""
    if not command:
        return None
    return SubprocessLLM(command, timeout=timeout)


def build_api_renderer(
    *,
    provider: str = "openai",
    model: str = "",
    endpoint: str = "",
    api_key_env: str = "",
    timeout: int = 120,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    min_chars: int = MIN_OUTPUT_CHARS,
    extra_headers: Optional[Dict[str, str]] = None,
    request_body: Optional[Dict[str, Any]] = None,
) -> ApiLLM:
    """Return an API-backed renderer."""
    return ApiLLM(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key_env=api_key_env,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        min_chars=min_chars,
        extra_headers=extra_headers,
        request_body=request_body,
    )


def build_renderer_from_config(config, *, mode: Optional[str] = None) -> Optional[Any]:
    """Build a renderer from a Config object.

    ``mode`` may be ``api`` or ``subprocess`` to force one transport. By
    default an API renderer is preferred when ``scheduler.llm_api`` carries a
    ``provider`` or an ``endpoint``, and the subprocess renderer is used when
    only ``scheduler.llm_command`` is set. Returns None when neither is
    configured, which is how the deterministic composer stays the default.
    """
    api_section = config.section("scheduler.llm_api") if hasattr(config, "section") else {}
    api_configured = bool(
        str(api_section.get("provider") or "").strip()
        or str(api_section.get("endpoint") or "").strip()
    )
    command = config.list("scheduler.llm_command") if hasattr(config, "list") else []

    selected_mode = (mode or "").strip().lower()
    if selected_mode not in {"api", "subprocess"}:
        selected_mode = "api" if api_configured else ("subprocess" if command else "")

    timeout = config.int("scheduler.llm_timeout_seconds", 120, minimum=5, maximum=900)

    if selected_mode == "api":
        return build_api_renderer(
            provider=str(api_section.get("provider") or "openai"),
            model=str(api_section.get("model") or ""),
            endpoint=str(api_section.get("endpoint") or ""),
            api_key_env=str(api_section.get("api_key_env") or ""),
            timeout=timeout,
            max_tokens=config.int("scheduler.llm_api.max_tokens", 2048, minimum=1, maximum=32768),
            temperature=float(api_section.get("temperature") or 0.7),
        )

    if selected_mode == "subprocess":
        if not command:
            raise LLMError("scheduler.llm_command is empty; set it to a local CLI")
        return SubprocessLLM(command, timeout=timeout)

    return None


def executable_from_path(candidate: str) -> bool:
    return Path(candidate).exists() or shutil.which(candidate) is not None


#: Default endpoints for well-known providers, so a config only has to name a
#: provider and a model. Anything else must supply `endpoint` explicitly.
DEFAULT_API_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "ollama": "http://localhost:11434/api/chat",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "together": "https://api.together.xyz/v1/chat/completions",
    "perplexity": "https://api.perplexity.ai/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}


class ApiLLM:
    """Callable renderer backed by an HTTP API endpoint.

    Understands the Anthropic messages shape and the OpenAI chat-completions
    shape (which covers OpenRouter, Groq, Mistral, Together, vLLM, LiteLLM,
    llamafile and every other compatible gateway). The API key comes from the
    environment variable named in ``api_key_env`` and is never logged.
    """

    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str = "",
        endpoint: str = "",
        api_key_env: str = "",
        timeout: int = 120,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        min_chars: int = MIN_OUTPUT_CHARS,
        extra_headers: Optional[Dict[str, str]] = None,
        request_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.provider = (provider or "openai").strip().lower() or "openai"
        self.model = (model or "").strip()
        self.endpoint = (endpoint or "").strip() or DEFAULT_API_ENDPOINTS.get(self.provider, "")
        self.api_key_env = (api_key_env or "").strip()
        self.timeout = max(int(timeout), 5)
        self.max_tokens = max(int(max_tokens), 1)
        self.temperature = float(temperature)
        self.min_chars = max(int(min_chars), 1)
        self.extra_headers = dict(extra_headers or {})
        self.request_body = dict(request_body or {})
        self.kind = "api"

        if not self.endpoint:
            raise LLMError(
                f"llm_api.endpoint is empty and there is no default for provider {self.provider!r}"
            )
        if self.provider == "anthropic" and not self.api_key_env:
            self.api_key_env = "ANTHROPIC_API_KEY"
        if self.provider == "openai" and not self.api_key_env:
            self.api_key_env = "OPENAI_API_KEY"

    def describe(self) -> str:
        return f"{self.provider}:{self.model or '(default)'} -> {self.endpoint}"

    def __call__(self, prompt: str) -> str:
        api_key = os.environ.get(self.api_key_env or "", "").strip() if self.api_key_env else ""
        if self.provider != "ollama" and not api_key:
            raise LLMError(
                f"environment variable {self.api_key_env!r} (llm_api.api_key_env) is not set"
            )

        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requests is a runtime dependency
            raise LLMError("requests is required to use the LLM API renderer") from exc

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "bot-for-opensource-funding/llm",
        }
        payload: Dict[str, Any]
        if self.provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-07"
            payload = {
                "model": self.model or "claude-3-5-sonnet-latest",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": self.model or "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        payload.update(self.request_body)
        headers.update(self.extra_headers)

        try:
            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - surface as a renderer failure, not a crash
            raise LLMError(f"renderer HTTP request failed: {exc}") from exc

        if response.status_code >= 400:
            body = (response.text or "").strip()[:400]
            raise LLMError(f"renderer HTTP error {response.status_code}: {body}")

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"renderer returned a non-JSON response: {exc}") from exc

        content = self._extract_content(data)
        output = (content or "").strip()
        if not output:
            raise LLMError("renderer HTTP response contained no content")
        if len(output) < self.min_chars:
            raise LLMError(f"renderer output too short ({len(output)} chars) - treating as failure")
        return output

    @staticmethod
    def _extract_content(data: Any) -> str:
        """Pull the assistant text out of Anthropic, OpenAI or Ollama shapes."""
        if isinstance(data, dict):
            if isinstance(data.get("content"), list):
                for block in data["content"]:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        return block["text"]
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                message = first.get("message") or first.get("delta") or {}
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
            message = data.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(data.get("response"), str):
                return data["response"]
            if isinstance(data.get("text"), str):
                return data["text"]
        return ""


__all__ = [
    "ApiLLM",
    "LLMError",
    "MIN_OUTPUT_CHARS",
    "SubprocessLLM",
    "build_api_renderer",
    "build_renderer",
    "build_renderer_from_config",
    "executable_from_path",
]
