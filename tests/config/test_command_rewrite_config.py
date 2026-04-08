"""Tests for CommandRewriteConfig schema.

CommandRewriteConfig is a cross-cutting tool-argument rewrite hook config.
Current (and so-far only) strategy: rtk command compression.
"""

from nanobot.config.schema import CommandRewriteConfig, ToolsConfig


def test_defaults_all_off() -> None:
    cfg = CommandRewriteConfig()
    assert cfg.enabled is False
    assert cfg.verbose is False
    assert cfg.timeout == 5.0


def test_camel_case_parsing() -> None:
    """Config JSON uses camelCase; schema must accept it via ToolsConfig."""
    tools = ToolsConfig.model_validate(
        {
            "commandRewrite": {
                "enabled": True,
                "verbose": True,
                "timeout": 7.5,
            }
        }
    )
    assert tools.command_rewrite.enabled is True
    assert tools.command_rewrite.verbose is True
    assert tools.command_rewrite.timeout == 7.5


def test_enabled_with_custom_timeout() -> None:
    cfg = CommandRewriteConfig(enabled=True, timeout=10.0)
    assert cfg.enabled is True
    assert cfg.verbose is False
    assert cfg.timeout == 10.0
