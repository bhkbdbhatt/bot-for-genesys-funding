"""LLM renderers: factory selection, HTTP API transport, fail-soft fallback.

The API tests monkeypatch ``requests.post``; nothing here touches the network.
"""

from __future__ import annotations

import pytest

from llm import ApiLLM, LLMError, SubprocessLLM, build_renderer_from_config
from content_engine import generate

OPENAI_KEY_ENV = "TEST_OPENAI_KEY"


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


def _openai_reply(text):
    return FakeResponse({"choices": [{"message": {"content": text}}]})


def _api_config(config_factory, sample_content, **llm_api):
    def mutate(document):
        document["scheduler"] = dict(document.get("scheduler") or {})
        document["scheduler"]["llm_api"] = llm_api

    return config_factory("llm-api", content_cfg=sample_content, mutate=mutate)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_no_renderer_configured_is_none(config_factory, sample_content):
    def mutate(document):
        document["scheduler"] = {"llm_command": [], "llm_api": {}}

    config = config_factory("llm-none", content_cfg=sample_content, mutate=mutate)
    assert build_renderer_from_config(config) is None


def test_empty_llm_api_block_does_not_shadow_the_subprocess(config_factory, sample_content):
    """The shipped example ships an empty llm_api block; it must stay inert."""
    def mutate(document):
        document["scheduler"] = {
            "llm_command": ["echo", "hi"],
            "llm_api": {"provider": "", "model": "", "endpoint": "", "api_key_env": ""},
        }

    config = config_factory("llm-inert", content_cfg=sample_content, mutate=mutate)
    renderer = build_renderer_from_config(config)
    assert isinstance(renderer, SubprocessLLM)
    assert renderer.describe() == "echo hi"


def test_api_provider_selects_the_api_renderer(config_factory, sample_content):
    config = _api_config(
        config_factory, sample_content,
        provider="openai", model="gpt-4o-mini", api_key_env=OPENAI_KEY_ENV,
    )
    renderer = build_renderer_from_config(config)
    assert isinstance(renderer, ApiLLM)
    assert renderer.endpoint == "https://api.openai.com/v1/chat/completions"
    assert renderer.model == "gpt-4o-mini"


def test_mode_forces_the_subprocess_renderer(config_factory, sample_content):
    def mutate(document):
        document["scheduler"] = {
            "llm_command": ["echo", "hi"],
            "llm_api": {"provider": "openai", "api_key_env": OPENAI_KEY_ENV},
        }

    config = config_factory("llm-both", content_cfg=sample_content, mutate=mutate)
    assert isinstance(build_renderer_from_config(config), ApiLLM)
    assert isinstance(build_renderer_from_config(config, mode="subprocess"), SubprocessLLM)
    assert isinstance(build_renderer_from_config(config, mode="api"), ApiLLM)


def test_unknown_provider_is_rejected_by_the_config(config_factory, sample_content):
    """The provider selects the wire format, so a typo must fail loudly."""
    from config_loader import ConfigError

    with pytest.raises(ConfigError, match="llm_api.provider"):
        _api_config(config_factory, sample_content, provider="mygateway")


def test_subprocess_mode_without_a_command_is_an_error(config_factory, sample_content):
    def mutate(document):
        document["scheduler"] = {"llm_command": []}

    config = config_factory("llm-sub", content_cfg=sample_content, mutate=mutate)
    with pytest.raises(LLMError, match="llm_command"):
        build_renderer_from_config(config, mode="subprocess")


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #


def test_openai_shape_returns_the_message(monkeypatch):
    import os

    os.environ[OPENAI_KEY_ENV] = "sk-test-key"
    try:
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.update(url=url, json=json, headers=headers, timeout=timeout)
            return _openai_reply("# Rendered\n\nbody text")

        monkeypatch.setattr("requests.post", fake_post)
        renderer = ApiLLM(provider="openai", model="gpt-4o-mini", api_key_env=OPENAI_KEY_ENV, min_chars=1)
        assert renderer("write it") == "# Rendered\n\nbody text"
        assert seen["url"] == "https://api.openai.com/v1/chat/completions"
        assert seen["headers"]["Authorization"] == "Bearer sk-test-key"
        assert seen["json"]["messages"][0]["content"] == "write it"
        assert seen["json"]["model"] == "gpt-4o-mini"
    finally:
        os.environ.pop(OPENAI_KEY_ENV, None)


def test_missing_api_key_is_an_llm_error(monkeypatch):
    import os

    os.environ.pop(OPENAI_KEY_ENV, None)
    monkeypatch.setattr("requests.post", lambda *a, **k: pytest.fail("must not POST"))
    renderer = ApiLLM(provider="openai", api_key_env=OPENAI_KEY_ENV)
    with pytest.raises(LLMError, match=OPENAI_KEY_ENV):
        renderer("prompt")


def test_http_error_is_an_llm_error(monkeypatch):
    import os

    os.environ[OPENAI_KEY_ENV] = "sk-test-key"
    try:
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: FakeResponse({"error": "boom"}, status_code=500, text="server exploded"),
        )
        renderer = ApiLLM(provider="openai", api_key_env=OPENAI_KEY_ENV)
        with pytest.raises(LLMError, match="500"):
            renderer("prompt")
    finally:
        os.environ.pop(OPENAI_KEY_ENV, None)


def test_anthropic_shape_and_headers(monkeypatch):
    import os

    os.environ["ANTHROPIC_API_KEY"] = "ant-key"
    try:
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.update(url=url, json=json, headers=headers)
            return FakeResponse({"content": [{"type": "text", "text": "anthropic reply"}]})

        monkeypatch.setattr("requests.post", fake_post)
        renderer = ApiLLM(provider="anthropic", model="claude-3-5-sonnet-latest", min_chars=1)
        assert renderer("prompt") == "anthropic reply"
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["headers"]["x-api-key"] == "ant-key"
        assert seen["headers"]["anthropic-version"]
        assert "Authorization" not in seen["headers"]
        assert seen["json"]["max_tokens"] == 2048
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_ollama_needs_no_api_key(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return FakeResponse({"message": {"role": "assistant", "content": "local reply"}})

    monkeypatch.setattr("requests.post", fake_post)
    renderer = ApiLLM(provider="ollama", model="llama3.1", min_chars=1)
    assert renderer("prompt") == "local reply"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert "Authorization" not in seen["headers"]


def test_short_output_is_treated_as_failure(monkeypatch):
    import os

    os.environ[OPENAI_KEY_ENV] = "sk-test-key"
    try:
        monkeypatch.setattr("requests.post", lambda *a, **k: _openai_reply("too short"))
        renderer = ApiLLM(provider="openai", api_key_env=OPENAI_KEY_ENV, min_chars=40)
        with pytest.raises(LLMError, match="too short"):
            renderer("prompt")
    finally:
        os.environ.pop(OPENAI_KEY_ENV, None)


# --------------------------------------------------------------------------- #
# generate() contract
# --------------------------------------------------------------------------- #


def test_generate_uses_the_renderer_and_labels_it(sample_content):
    seen = {}

    def renderer(prompt):
        seen["prompt"] = prompt
        return "# Rendered title\n\n## one\n\n## two\n\n" + ("word " * 400)

    draft = generate(sample_content, platform="devto", llm_renderer=renderer)
    assert draft["renderer"] == "llm"
    assert draft["title"] == "Rendered title"
    assert "HARD RULES" in seen["prompt"]


def test_generate_falls_back_when_the_renderer_fails(sample_content):
    def renderer(prompt):
        raise LLMError("network down")

    draft = generate(sample_content, platform="devto", llm_renderer=renderer)
    assert draft["renderer"] == "deterministic"
    assert draft["body"].startswith("# ")


def test_source_text_reaches_the_renderer(sample_content):
    seen = {}

    def renderer(prompt):
        seen["prompt"] = prompt
        return "# Rendered title\n\n## one\n\n## two\n\n" + ("word " * 400)

    generate(
        sample_content,
        platform="devto",
        llm_renderer=renderer,
        source_text="# From the README\n\nreadme-only detail",
    )
    assert "SOURCE DOCUMENT (readme/brief material):" in seen["prompt"]
    assert "readme-only detail" in seen["prompt"]
