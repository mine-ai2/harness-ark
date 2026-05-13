"""OpenRouter provider — thin subclass of OpenAIProvider with a default base URL."""

from ark.provider import OpenAIProvider, OpenRouterProvider
from ark.runtime import make_provider


def test_openrouter_defaults_to_openrouter_base_url():
    p = OpenRouterProvider(api_key="or-test")
    assert isinstance(p, OpenAIProvider)
    # The underlying AsyncOpenAI client exposes base_url; it should match the OpenRouter default.
    assert str(p._client.base_url).rstrip("/") == OpenRouterProvider.DEFAULT_BASE_URL.rstrip("/")


def test_openrouter_respects_explicit_base_url():
    p = OpenRouterProvider(api_key="or-test", base_url="https://custom.example.com/v1")
    assert str(p._client.base_url).rstrip("/") == "https://custom.example.com/v1"


def test_make_provider_dispatches_openrouter():
    p = make_provider("openrouter", api_key="or-test")
    assert isinstance(p, OpenRouterProvider)


def test_make_provider_unknown_raises():
    import pytest

    with pytest.raises(ValueError, match="unsupported provider"):
        make_provider("nope", api_key="x")
