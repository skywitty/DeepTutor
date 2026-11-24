"""Orchestrates the math animator generation flow."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Callable

from deeptutor.agents._shared.history_budget import (
    MIN_HISTORY_TOKENS,
    MIN_OUTPUT_TOKENS,
    PROMPT_OVERHEAD_TOKENS,
    attachment_tokens,
    count_text_tokens,
    fit_history_context,
    plan_stage_budget,
)
from deeptutor.core.context import Attachment
from deeptutor.services.llm.config import get_llm_config
from deeptutor.services.llm.exceptions import ProviderContextWindowError
from deeptutor.services.model_selection.task_agent import task_model_agent

from .agents import (
    CodeGeneratorAgent,
    ConceptAnalysisAgent,
    ConceptDesignAgent,
    SummaryAgent,
    VisualReviewAgent,
)
from .duration_utils import parse_target_duration_seconds
from .models import RenderResult, VisualReviewResult
from .renderer import ManimRenderError, ManimRenderService
from .request_config import MathAnimatorRequestConfig
from .retry_manager import CodeRetryManager
from .visual_review import VisualReviewService

logger = logging.getLogger(__name__)

# Layout repair rounds checked before rendering (a dry run, seconds each).
MAX_LAYOUT_ROUNDS = 3


def _configured_context_window() -> int | None:
    """The deployment's declared window, when it declares one.

    Read lazily and defensively: a stage must be able to plan its prompt
    before any settings file exists, and a missing declaration is the normal
    case, not an error — :func:`plan_stage_budget` then falls back to the
    model's known capacity.
    """
    try:
        value = getattr(get_llm_config(), "context_window", None)
    except Exception:
        return None
    return value if isinstance(value, int) and value > 0 else None


class MathAnimatorPipeline:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        api_version: str | None,
        language: str = "zh",
        trace_callback: Callable[[dict[str, Any]], Any] | None = None,
        enable_visual_review: bool = False,
        workspace_output_dir: str | Path | None = None,
        workspace_root: str | Path | None = None,
        workspace_id: str = "",
    ) -> None:
        self.enable_visual_review = enable_visual_review
        self.workspace_output_dir = (
            Path(workspace_output_dir).resolve() if workspace_output_dir else None
        )
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.workspace_id = workspace_id
        self.analysis_agent = ConceptAnalysisAgent(
            api_key=api_key,
            base_url=base_url,
            api_version=api_version,
            language=language,
        )
        self.design_agent = ConceptDesignAgent(
            api_key=api_key,
            base_url=base_url,
            api_version=api_version,
            language=language,
        )
        self.code_agent = CodeGeneratorAgent(
            api_key=api_key,
            base_url=base_url,
            api_version=api_version,
            language=language,
        )
        # Repairs are targeted edits to a working script; the task model does them in
        # seconds where a reasoning chat model can take several minutes.
        self.repair_agent = (
            task_model_agent(lambda **kw: CodeGeneratorAgent(language=language, **kw))
            or self.code_agent
        )
        self.summary_agent = SummaryAgent(
            api_key=api_key,
            base_url=base_url,
            api_version=api_version,
            language=language,
        )
        self.visual_review_agent = (
            VisualReviewAgent(
                api_key=api_key,
                base_url=base_url,
                api_version=api_version,
                language=language,
            )
            if enable_visual_review
            else None
        )
        self.set_trace_callback(trace_callback)

    def set_trace_callback(self, callback: Callable[[dict[str, Any]], Any] | None) -> None:
        for agent in (
            self.analysis_agent,
            self.design_agent,
            self.code_agent,
            self.repair_agent,
            self.summary_agent,
            self.visual_review_agent,
        ):
            if agent is not None:
                agent.set_trace_callback(callback)

    async def run_analysis(
        self,
        *,
        user_input: str,
        history_context: str,
        request_config: MathAnimatorRequestConfig,
        attachments: list[Attachment],
    ):
        """Plan the analysis call against the window this model can actually serve.

        The history arriving here was budgeted for the *session*, against the
        advertised window, and the analysis stage asks for the capability's
        full output reserve on top of it. On a small or un-annotated model the
        two together exceed the window and the provider refuses the request
        before the model sees the prompt, so the turn dies with a
        context-length error the user cannot act on. The reserve is taken
        first, the history gets what is left, and the newest turns are kept.
        """
        budget = plan_stage_budget(
            model=str(self.analysis_agent.get_model() or ""),
            context_window=_configured_context_window(),
            output_tokens=int(self.analysis_agent.get_max_tokens() or 0),
            prompt_tokens=count_text_tokens(user_input) + PROMPT_OVERHEAD_TOKENS,
            image_tokens=attachment_tokens(attachments),
        )
        fitted = fit_history_context(history_context, budget.history_budget)
        if fitted.dropped_blocks:
            logger.warning(
                "Math animator analysis history trimmed to %d tokens: dropped %d block(s) "
                "(window %d, output reserve %d, prompt overhead %d).",
                budget.history_budget,
                fitted.dropped_blocks,
                budget.window,
                budget.output_tokens,
                PROMPT_OVERHEAD_TOKENS,
            )
        try:
            return await self.analysis_agent.process(
                user_input=user_input,
                history_context=fitted.text,
                output_mode=request_config.output_mode,
                style_hint=request_config.style_hint,
                attachments=attachments,
                max_tokens=budget.output_tokens,
            )
        except ProviderContextWindowError as exc:
            # The window planned above is an estimate; the provider's refusal is
            # the real limit. Give the turn one more attempt on the floor, with
            # the current request and the newest turns only.
            emergency = fit_history_context(history_context, MIN_HISTORY_TOKENS)
            if emergency.text == fitted.text:
                raise
            logger.warning(
                "Math animator analysis rejected by the provider (%s); retrying once with "
                "%d tokens of history and a %d-token output reserve.",
                exc,
                MIN_HISTORY_TOKENS,
                MIN_OUTPUT_TOKENS,
            )
            return await self.analysis_agent.process(
                user_input=user_input,
                history_context=emergency.text,
                output_mode=request_config.output_mode,
                style_hint=request_config.style_hint,
                attachments=attachments,
                max_tokens=MIN_OUTPUT_TOKENS,
            )

    async def run_design(
        self,
        *,
        user_input: str,
        request_config: MathAnimatorRequestConfig,
        analysis,
    ):
        return await self.design_agent.process(
            user_input=user_input,
            output_mode=request_config.output_mode,
            analysis=analysis,
            style_hint=request_config.style_hint,
        )

    async def run_code_generation(
        self,
        *,
        user_input: str,
        request_config: MathAnimatorRequestConfig,
        analysis,
        design,
    ):
        duration_target_seconds = parse_target_duration_seconds(
            " ".join(
                part.strip()
                for part in (user_input, request_config.style_hint)
                if isinstance(part, str) and part.strip()
            )
        )
        return await self.code_agent.generate(
            user_input=user_input,
            output_mode=request_config.output_mode,
            analysis=analysis,
            design=design,
            duration_target_seconds=duration_target_seconds,
        )

    async def run_render(
        self,
        *,
        turn_id: str,
        user_input: str,
        request_config: MathAnimatorRequestConfig,
        initial_code: str,
        on_retry: Callable[[Any], Any] | None = None,
        on_render_progress: Callable[[str, bool], Any] | None = None,
        on_retry_status: Callable[[str], Any] | None = None,
    ) -> tuple[str, RenderResult]:
        renderer = ManimRenderService(
            turn_id,
            progress_callback=on_render_progress,
            output_dir=self.workspace_output_dir,
            workspace_root=self.workspace_root,
        )
        duration_target_seconds = parse_target_duration_seconds(
            " ".join(
                part.strip()
                for part in (user_input, request_config.style_hint)
                if isinstance(part, str) and part.strip()
            )
        )
        review_callback: Callable[[str, RenderResult], Any] | None = None
        if self.enable_visual_review and self.visual_review_agent is not None:
            review_service = VisualReviewService(
                turn_id,
                progress_callback=on_render_progress,
                output_dir=self.workspace_output_dir,
            )

            async def _review_callback(
                current_code: str, render_result: RenderResult
            ) -> VisualReviewResult:
                attachments = await review_service.build_attachments(render_result)
                return await self.visual_review_agent.process(
                    user_input=user_input,
                    output_mode=request_config.output_mode,
                    current_code=current_code,
                    render_result=render_result,
                    attachments=attachments,
                )

            review_callback = _review_callback
        elif request_config.output_mode == "video":
            # No vision model: measure the scene's geometry instead. The dry-run check takes
            # seconds, so it gates each render: a failing layout goes back for repair before
            # any frame is drawn. After the rounds run out the scene renders as it stands.
            from .layout_probe import geometry_review

            layout_rounds = {"left": MAX_LAYOUT_ROUNDS}
            render_scene = renderer.render

            async def _checked_render(*, code: str, output_mode: str, quality: str) -> RenderResult:
                if layout_rounds["left"] > 0:
                    review = await geometry_review(code)
                    if review is not None and not review.passed:
                        layout_rounds["left"] -= 1
                        raise ManimRenderError(
                            "Layout check (before rendering) failed. "
                            + review.summary
                            + " Suggested fix: "
                            + review.suggested_fix
                        )
                return await render_scene(code=code, output_mode=output_mode, quality=quality)

            renderer.render = _checked_render  # type: ignore[method-assign]

            async def _geometry_callback(
                current_code: str, render_result: RenderResult
            ) -> VisualReviewResult | None:
                # Record-only: what the rendered scene still has, for the result payload.
                review = await geometry_review(current_code)
                if review is None:
                    return None
                if not review.passed:
                    review.summary = "Layout issues remain after repair rounds: " + review.summary
                return review

            review_callback = _geometry_callback

        retry_manager = CodeRetryManager(
            renderer=renderer,
            max_retries=4,
            on_retry=on_retry,
            on_status=on_retry_status,
            review_callback=review_callback,
            repair_callback=lambda current_code, error_message, attempt: self.repair_agent.repair(
                user_input=user_input,
                output_mode=request_config.output_mode,
                current_code=current_code,
                error_message=error_message,
                attempt=attempt,
                duration_target_seconds=duration_target_seconds,
            ),
        )
        final_code, render_result = await retry_manager.render_with_retries(
            initial_code=initial_code,
            output_mode=request_config.output_mode,
            quality=request_config.quality,
        )
        result = RenderResult.model_validate(render_result.model_dump())
        self._publish_workspace_artifacts(result)
        return final_code, result

    def _publish_workspace_artifacts(self, render_result: RenderResult) -> None:
        """Snapshot final Manim files into the universal presentation layer."""
        if not self.workspace_id or self.workspace_root is None:
            return
        rows = [
            {
                "path": artifact.relative_path,
                "title": artifact.label or artifact.filename,
            }
            for artifact in render_result.artifacts
            if artifact.relative_path
        ]
        if not rows:
            return
        from deeptutor.services.workspace import get_content_workspace_service

        service = get_content_workspace_service()
        binding = service.binding_by_id(self.workspace_id)
        items = service.publish(binding, rows)
        by_path = {item.relative_path: item for item in items}
        for artifact in render_result.artifacts:
            item = by_path.get(artifact.relative_path)
            if item is not None:
                artifact.url = item.url
        render_result.workspace_items = [item.to_dict() for item in items]

    async def run_summary(
        self,
        *,
        user_input: str,
        request_config: MathAnimatorRequestConfig,
        analysis,
        design,
        render_result: RenderResult,
    ):
        return await self.summary_agent.process(
            user_input=user_input,
            output_mode=request_config.output_mode,
            analysis=analysis,
            design=design,
            render_result=render_result,
        )

    async def run(
        self,
        *,
        turn_id: str,
        user_input: str,
        history_context: str,
        request_config: MathAnimatorRequestConfig,
        attachments: list[Attachment],
    ) -> dict[str, Any]:
        timings: dict[str, float] = {}

        start = time.perf_counter()
        analysis = await self.run_analysis(
            user_input=user_input,
            history_context=history_context,
            request_config=request_config,
            attachments=attachments,
        )
        timings["concept_analysis"] = round(time.perf_counter() - start, 3)

        start = time.perf_counter()
        design = await self.run_design(
            user_input=user_input,
            request_config=request_config,
            analysis=analysis,
        )
        timings["concept_design"] = round(time.perf_counter() - start, 3)

        start = time.perf_counter()
        generated = await self.run_code_generation(
            user_input=user_input,
            request_config=request_config,
            analysis=analysis,
            design=design,
        )
        timings["code_generation"] = round(time.perf_counter() - start, 3)

        start = time.perf_counter()
        final_code, render_result = await self.run_render(
            turn_id=turn_id,
            user_input=user_input,
            request_config=request_config,
            initial_code=generated.code,
        )
        timings["code_retry"] = round(time.perf_counter() - start, 3)

        start = time.perf_counter()
        summary = await self.run_summary(
            user_input=user_input,
            request_config=request_config,
            analysis=analysis,
            design=design,
            render_result=render_result,
        )
        timings["summary"] = round(time.perf_counter() - start, 3)

        timings["render_output"] = timings["code_retry"]
        return {
            "analysis": analysis,
            "design": design,
            "code": final_code,
            "render_result": render_result,
            "summary": summary,
            "timings": timings,
        }


__all__ = ["MathAnimatorPipeline"]
