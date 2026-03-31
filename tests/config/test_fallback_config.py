"""Test fallback model configuration parsing."""
from nanobot.config.schema import AgentDefaults


def test_defaults_have_empty_fallback_models():
    d = AgentDefaults()
    assert d.fallback_models == []
    assert d.fallback_cooldown_s == 60


def test_fallback_models_from_camel_case():
    d = AgentDefaults.model_validate({
        "fallbackModels": ["openrouter/anthropic/claude-sonnet-4", "deepseek/deepseek-chat"],
        "fallbackCooldownS": 30,
    })
    assert d.fallback_models == ["openrouter/anthropic/claude-sonnet-4", "deepseek/deepseek-chat"]
    assert d.fallback_cooldown_s == 30
