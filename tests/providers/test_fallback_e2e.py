"""End-to-end test: AgentRunner → FallbackProvider → primary fails → fallback succeeds.

Simulates the exact call path that happens in production:
  AgentRunner.run() → provider.chat_stream_with_retry() → FallbackProvider.chat_stream()
  AgentRunner.run() → provider.chat_with_retry() → FallbackProvider.chat()
"""
import asyncio

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.providers.fallback import FallbackProvider


class MockProvider(LLMProvider):
    """Provider with controllable responses."""

    def __init__(self, responses: list, default_model: str = "mock-primary"):
        super().__init__()
        self._responses = list(responses)
        self._default_model = default_model
        self.calls = 0
        self.call_models: list[str | None] = []

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        self.call_models.append(kwargs.get("model"))
        if not self._responses:
            return LLMResponse(content="exhausted", finish_reason="error")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    async def chat_stream(self, **kwargs) -> LLMResponse:
        # 模拟真实 provider：chat_stream 和 chat 走同一逻辑
        on_delta = kwargs.pop("on_content_delta", None)
        resp = await self.chat(**kwargs)
        if on_delta and resp.content and resp.finish_reason != "error":
            await on_delta(resp.content)
        return resp

    def get_default_model(self) -> str:
        return self._default_model


def _ok(content="ok"):
    return LLMResponse(content=content)


def _transient_429():
    return LLMResponse(
        content='Error calling LLM: Error code: 429 - {\'type\': \'error\', \'error\': {\'type\': \'rate_limit_error\'}}',
        finish_reason="error",
    )


def _transient_529():
    return LLMResponse(
        content='Error calling LLM: Error code: 529 - {\'type\': \'error\', \'error\': {\'type\': \'overloaded_error\', \'message\': \'Overloaded\'}}',
        finish_reason="error",
    )


def _make_spec(messages=None) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=messages or [
            {"role": "system", "content": "You are a test bot."},
            {"role": "user", "content": "hi"},
        ],
        tools=ToolRegistry(),
        model="mock-primary",
        max_iterations=1,
    )


# ============================================================================
# Test: AgentRunner → chat_with_retry → FallbackProvider (non-streaming)
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_non_streaming_fallback_on_429():
    """AgentRunner non-streaming: primary 429 → fallback succeeds."""
    # primary 会被 chat_with_retry 重试 3 次 + 最终 1 次 = 最多 4 次 chat() 调用
    # 但 FallbackProvider.chat() 第一次就触发 fallback
    primary = MockProvider([_transient_429()] * 10)  # 给够多的 429 响应
    fallback = MockProvider([_ok("fallback saved the day")])
    provider = FallbackProvider(primary, [(fallback, "fb-model")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    result = await runner.run(_make_spec())

    assert result.final_content == "fallback saved the day"
    assert result.stop_reason == "completed"
    assert fallback.calls >= 1
    # fallback 收到的 model 应该是 "fb-model"
    assert "fb-model" in fallback.call_models


@pytest.mark.asyncio
async def test_agent_runner_non_streaming_fallback_on_529():
    """AgentRunner non-streaming: primary 529 overloaded → fallback succeeds."""
    primary = MockProvider([_transient_529()] * 10)
    fallback = MockProvider([_ok("fallback on 529")])
    provider = FallbackProvider(primary, [(fallback, "fb-model")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    result = await runner.run(_make_spec())

    assert result.final_content == "fallback on 529"
    assert result.stop_reason == "completed"


# ============================================================================
# Test: AgentRunner → chat_stream_with_retry → FallbackProvider (streaming)
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_streaming_fallback_on_429():
    """AgentRunner streaming: primary 429 → fallback succeeds."""
    from nanobot.agent.hook import AgentHook

    class StreamHook(AgentHook):
        def wants_streaming(self): return True
        async def on_stream(self, ctx, delta): pass
        async def on_stream_end(self, ctx, *, resuming): pass

    primary = MockProvider([_transient_429()] * 10)
    fallback = MockProvider([_ok("stream fallback ok")])
    provider = FallbackProvider(primary, [(fallback, "fb-model")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    spec = _make_spec()
    spec.hook = StreamHook()
    result = await runner.run(spec)

    assert result.final_content == "stream fallback ok"
    assert result.stop_reason == "completed"


# ============================================================================
# Test: chain fallback — first fallback also fails
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_chain_fallback():
    """Primary 429 → fallback[0] 503 → fallback[1] succeeds."""
    primary = MockProvider([_transient_429()] * 10)
    fb1 = MockProvider([_transient_529()] * 10)
    fb2 = MockProvider([_ok("third time's the charm")])
    provider = FallbackProvider(primary, [(fb1, "fb1"), (fb2, "fb2")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    result = await runner.run(_make_spec())

    assert result.final_content == "third time's the charm"
    assert fb2.calls >= 1


# ============================================================================
# Test: all providers fail → error propagated
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_all_fail_returns_error():
    """Primary + all fallbacks fail → AgentRunner gets error."""
    primary = MockProvider([_transient_429()] * 10)
    fallback = MockProvider([_transient_529()] * 10)
    provider = FallbackProvider(primary, [(fallback, "fb-model")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    result = await runner.run(_make_spec())

    # 全挂了应该返回错误
    assert result.stop_reason == "error"
    assert result.error is not None


# ============================================================================
# Test: circuit breaker — second call skips primary
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_circuit_breaker_second_call():
    """After primary fails, second AgentRunner.run() skips primary (cooldown)."""
    primary = MockProvider([_transient_429()] * 2 + [_ok("primary ok")])
    fallback = MockProvider([_ok("fb call 1"), _ok("fb call 2")])
    provider = FallbackProvider(primary, [(fallback, "fb-model")], cooldown_s=60)
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)

    # 第一次：primary 失败 → fallback
    r1 = await runner.run(_make_spec())
    assert r1.final_content == "fb call 1"
    primary_calls_after_first = primary.calls

    # 第二次：cooldown 期间跳过 primary
    r2 = await runner.run(_make_spec())
    assert r2.final_content == "fb call 2"
    assert primary.calls == primary_calls_after_first  # primary 没有被调用


# ============================================================================
# Test: primary succeeds — no fallback involved
# ============================================================================

@pytest.mark.asyncio
async def test_agent_runner_primary_ok_no_fallback():
    """When primary works, fallback is never called."""
    primary = MockProvider([_ok("primary is fine")])
    fallback = MockProvider([_ok("should not reach")])
    provider = FallbackProvider(primary, [(fallback, "fb-model")])
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=100)

    runner = AgentRunner(provider)
    result = await runner.run(_make_spec())

    assert result.final_content == "primary is fine"
    assert fallback.calls == 0
