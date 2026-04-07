"""Test that _make_provider wraps with FallbackProvider when fallback_models configured."""
from nanobot.config.schema import Config


def _make_config(fallback_models=None, cooldown=60):
    """Build a minimal Config with anthropic key + optional fallbacks."""
    return Config.model_validate({
        "providers": {
            "anthropic": {"apiKey": "sk-test"},
            "openrouter": {"apiKey": "sk-or-test"},
        },
        "agents": {
            "defaults": {
                "model": "anthropic/claude-opus-4-5",
                "fallbackModels": fallback_models or [],
                "fallbackCooldownS": cooldown,
            }
        }
    })


def test_no_fallback_returns_plain_provider():
    from nanobot.nanobot import _make_provider
    from nanobot.providers.fallback import FallbackProvider

    provider = _make_provider(_make_config())
    assert not isinstance(provider, FallbackProvider)


def test_with_fallback_returns_fallback_provider():
    from nanobot.nanobot import _make_provider
    from nanobot.providers.fallback import FallbackProvider

    provider = _make_provider(_make_config(
        fallback_models=["openrouter/anthropic/claude-sonnet-4"],
    ))
    assert isinstance(provider, FallbackProvider)
    assert len(provider.fallbacks) == 1
    assert provider._cooldown_s == 60


def test_fallback_with_multiple_models():
    """Multiple fallback models create a chain."""
    from nanobot.nanobot import _make_provider
    from nanobot.providers.fallback import FallbackProvider

    provider = _make_provider(_make_config(
        fallback_models=["openrouter/anthropic/claude-sonnet-4", "openrouter/deepseek/deepseek-chat"],
    ))
    assert isinstance(provider, FallbackProvider)
    assert len(provider.fallbacks) == 2


def test_cooldown_propagated():
    """Custom cooldown_s is propagated to FallbackProvider."""
    from nanobot.nanobot import _make_provider
    from nanobot.providers.fallback import FallbackProvider

    provider = _make_provider(_make_config(
        fallback_models=["openrouter/anthropic/claude-sonnet-4"],
        cooldown=120,
    ))
    assert isinstance(provider, FallbackProvider)
    assert provider._cooldown_s == 120
