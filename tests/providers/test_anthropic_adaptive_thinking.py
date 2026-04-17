from __future__ import annotations

import pytest

from nanobot.providers.anthropic_provider import AnthropicProvider


def _build(model: str, reasoning_effort: str | None = None) -> dict:
    provider = AnthropicProvider(api_key="sk-ant-api03-fake")
    return provider._build_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model=model,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=reasoning_effort,
        tool_choice=None,
        supports_caching=False,
    )


# Per Anthropic guidance: intelligence-sensitive workloads should use high+ effort.
# Opus 4.x supports 'xhigh', Sonnet caps at 'high'.
_OPUS_DEFAULT_EFFORT = "xhigh"
_SONNET_DEFAULT_EFFORT = "high"


@pytest.mark.parametrize(
    "model,expected_effort",
    [
        ("claude-opus-4-6-20251010", _OPUS_DEFAULT_EFFORT),
        ("claude-opus-4.7", _OPUS_DEFAULT_EFFORT),
        ("claude-opus-4.7-20251010", _OPUS_DEFAULT_EFFORT),
        ("claude-sonnet-4-6-20250514", _SONNET_DEFAULT_EFFORT),
        ("claude-sonnet-4.7", _SONNET_DEFAULT_EFFORT),
        ("claude-sonnet-4.7-20251101", _SONNET_DEFAULT_EFFORT),
    ],
)
def test_adaptive_thinking_auto_enabled_for_4x(model: str, expected_effort: str) -> None:
    """Claude 4.x models should auto-enable adaptive thinking with high+ effort default."""
    kwargs = _build(model, reasoning_effort=None)
    assert kwargs.get("thinking") == {"type": "adaptive", "display": "summarized"}
    assert kwargs["output_config"]["effort"] == expected_effort
    assert "temperature" not in kwargs


def test_adaptive_thinking_explicit_display_summarized() -> None:
    """All adaptive calls must request summarized thinking — Opus 4.7 defaults to omitted."""
    for model in ("claude-opus-4.7", "claude-sonnet-4.6", "claude-opus-4-6"):
        kwargs = _build(model, reasoning_effort="medium")
        assert kwargs["thinking"]["display"] == "summarized", (
            f"{model} must opt-in to summarized display"
        )


def test_non_adaptive_model_uses_budget_thinking() -> None:
    """Older models (e.g. 3.7 Sonnet) should use budget-based thinking."""
    kwargs = _build("claude-3-7-sonnet-20250219", reasoning_effort="high")
    assert kwargs.get("thinking") == {"type": "enabled", "budget_tokens": 8192}
    assert "output_config" not in kwargs
    assert kwargs["temperature"] == 1.0


def test_opus_accepts_max_effort() -> None:
    """Opus 4.x should allow 'max' effort."""
    kwargs = _build("claude-opus-4.7", reasoning_effort="max")
    assert kwargs["output_config"]["effort"] == "max"


def test_opus_accepts_xhigh_effort() -> None:
    """Opus 4.x should allow 'xhigh' effort."""
    kwargs = _build("claude-opus-4.7", reasoning_effort="xhigh")
    assert kwargs["output_config"]["effort"] == "xhigh"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4.7",
        "claude-sonnet-4-7-20251101",
    ],
)
def test_sonnet_downgrades_max_to_high(model: str) -> None:
    """Non-Opus 4.x should downgrade 'max' to 'high' (Sonnet caps at high)."""
    kwargs = _build(model, reasoning_effort="max")
    assert kwargs["output_config"]["effort"] == "high"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4.7",
        "claude-sonnet-4-7-20251101",
    ],
)
def test_sonnet_downgrades_xhigh_to_high(model: str) -> None:
    """Sonnet caps at 'high' — both 'xhigh' and 'max' should downgrade."""
    kwargs = _build(model, reasoning_effort="xhigh")
    assert kwargs["output_config"]["effort"] == "high"


def test_no_thinking_when_reasoning_disabled() -> None:
    """When reasoning_effort is None and model is not adaptive, thinking is disabled."""
    kwargs = _build("claude-3-5-sonnet-20240620", reasoning_effort=None)
    assert "thinking" not in kwargs
    assert kwargs["temperature"] == 0.7
