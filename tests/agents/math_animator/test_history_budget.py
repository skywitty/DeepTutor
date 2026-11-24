"""The analysis stage must fit its prompt into the window the model can serve.

Two failures the session-level history budget cannot see: a stage asking for
more output tokens than the window leaves after its prompt, and a large
advertised window admitting a 100k-token prompt to a proxy route that serves
less. Both end the turn with a provider context-length error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deeptutor.agents._shared.history_budget import (
    MAX_WINDOW_RATIO,
    MIN_OUTPUT_TOKENS,
    attachment_tokens,
    count_text_tokens,
    fit_history_context,
    plan_stage_budget,
)
from deeptutor.agents.math_animator.pipeline import MathAnimatorPipeline
from deeptutor.agents.math_animator.request_config import MathAnimatorRequestConfig
from deeptutor.services.llm.exceptions import ProviderContextWindowError


def _history(blocks: int, chars: int = 4000) -> str:
    """Chronological blocks of the shape ``build_history_text`` produces."""
    return "\n\n".join(f"User: {'x' * chars}" for _ in range(blocks))


def test_output_reserve_is_clamped_to_what_the_window_leaves() -> None:
    # An un-annotated model name falls back to 16_384, and math_animator asks
    # for 16_834 output tokens: the request is impossible before the prompt is
    # counted, so the reserve has to shrink.
    budget = plan_stage_budget(
        model="mystery-model",
        output_tokens=16834,
        prompt_tokens=1500,
    )
    assert budget.window == 16384
    assert budget.output_tokens < budget.window
    assert budget.output_tokens >= MIN_OUTPUT_TOKENS
    assert budget.output_tokens + budget.history_budget + 1500 <= budget.window


def test_history_is_capped_for_a_large_window() -> None:
    # A 1M-token window does not make a 100k-token analysis prompt sensible:
    # the stage answers with a few hundred tokens of JSON.
    budget = plan_stage_budget(
        model="deepseek-v4",
        output_tokens=16834,
        prompt_tokens=1500,
    )
    assert budget.history_budget <= int(budget.window * MAX_WINDOW_RATIO)


def test_history_within_budget_is_sent_verbatim() -> None:
    text = "User: 之前讨论过函数图像\n\nAssistant: 抛物线的定义"
    fit = fit_history_context(text, 4000)
    assert fit.text == text
    assert fit.dropped_blocks == 0


def test_trim_keeps_the_summary_and_the_newest_turns() -> None:
    summary = "Conversation summary:\n用户一直在学二次函数。"
    old = "User: 很久以前的长问题\n" + "y" * 4000
    recent = "User: 最近的问题\n" + "z" * 4000
    text = "\n\n".join([summary, old, recent])

    fit = fit_history_context(text, 1200)

    assert fit.dropped_blocks == 1
    assert fit.text.startswith("Conversation summary:")
    assert "omitted" in fit.text
    assert "很久以前的长问题" not in fit.text
    assert "最近的问题" in fit.text
    assert count_text_tokens(fit.text) <= 1200


def test_summary_only_history_over_the_budget_is_counted_as_dropped() -> None:
    # A session whose whole history is one summary block can still overflow a
    # small window, and the stage must report the drop rather than silently
    # sending a prompt the provider will refuse.
    summary = "Conversation summary:\n" + "s" * 4000
    fit = fit_history_context(summary, 256)
    assert fit.dropped_blocks == 1
    assert fit.text.startswith("[1 earlier turn(s) omitted")
    assert "Conversation summary:" not in fit.text


def test_image_attachments_are_charged_to_the_input_budget() -> None:
    attachments = [
        SimpleNamespace(type="image"),
        SimpleNamespace(type="file"),
        SimpleNamespace(type="image"),
    ]
    assert attachment_tokens(attachments) == 2 * 4096
    assert attachment_tokens([]) == 0


def _pipeline(monkeypatch: pytest.MonkeyPatch, *, model: str, max_tokens: int):
    pipeline = MathAnimatorPipeline(
        api_key="k",
        base_url="u",
        api_version=None,
        language="zh",
    )
    calls: list[dict[str, object]] = []

    async def fake_process(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model_dump=lambda: {"learning_goal": "teach parabola"})

    # The window the stage plans against is the point of these tests, so the
    # deployment's declared window is taken out of the picture.
    monkeypatch.setattr(
        "deeptutor.agents.math_animator.pipeline._configured_context_window",
        lambda: None,
    )
    monkeypatch.setattr(pipeline.analysis_agent, "process", fake_process)
    monkeypatch.setattr(pipeline.analysis_agent, "get_model", lambda: model)
    monkeypatch.setattr(pipeline.analysis_agent, "get_max_tokens", lambda: max_tokens)
    return pipeline, calls


@pytest.mark.asyncio
async def test_analysis_sends_history_fitted_to_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, calls = _pipeline(monkeypatch, model="mystery-model", max_tokens=16834)

    await pipeline.run_analysis(
        user_input="讲解抛物线的图像",
        history_context=_history(20),
        request_config=MathAnimatorRequestConfig(),
        attachments=[],
    )

    sent = calls[0]["history_context"]
    assert "omitted" in sent
    assert count_text_tokens(sent) <= 256
    # The reserve the provider will accept, not the configured 16_834.
    assert calls[0]["max_tokens"] < 16834


@pytest.mark.asyncio
async def test_analysis_keeps_history_when_the_window_is_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, calls = _pipeline(monkeypatch, model="deepseek-v4", max_tokens=16834)
    history = _history(20)

    await pipeline.run_analysis(
        user_input="讲解抛物线的图像",
        history_context=history,
        request_config=MathAnimatorRequestConfig(),
        attachments=[],
    )

    assert calls[0]["history_context"] == history
    assert calls[0]["max_tokens"] == 16834


@pytest.mark.asyncio
async def test_context_window_error_retries_once_on_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, calls = _pipeline(monkeypatch, model="deepseek-v4", max_tokens=16834)

    async def reject_then_answer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ProviderContextWindowError(
                "This model's maximum context length is 8192 tokens, "
                "however your messages used 90000 tokens."
            )
        return SimpleNamespace(model_dump=lambda: {"learning_goal": "teach parabola"})

    monkeypatch.setattr(pipeline.analysis_agent, "process", reject_then_answer)

    result = await pipeline.run_analysis(
        user_input="讲解抛物线的图像",
        history_context=_history(20),
        request_config=MathAnimatorRequestConfig(),
        attachments=[],
    )

    assert len(calls) == 2
    first, second = calls[0]["history_context"], calls[1]["history_context"]
    assert count_text_tokens(second) < count_text_tokens(first)
    assert calls[1]["max_tokens"] == MIN_OUTPUT_TOKENS
    assert result.model_dump()["learning_goal"] == "teach parabola"


@pytest.mark.asyncio
async def test_second_context_window_error_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, calls = _pipeline(monkeypatch, model="deepseek-v4", max_tokens=16834)

    async def always_reject(**kwargs):
        calls.append(kwargs)
        raise ProviderContextWindowError("maximum context length is 8192 tokens")

    monkeypatch.setattr(pipeline.analysis_agent, "process", always_reject)

    with pytest.raises(ProviderContextWindowError):
        await pipeline.run_analysis(
            user_input="讲解抛物线的图像",
            history_context=_history(20),
            request_config=MathAnimatorRequestConfig(),
            attachments=[],
        )

    assert len(calls) == 2
