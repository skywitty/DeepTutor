"""Fitting the shared conversation history into one stage's context window.

A capability that feeds the session's history text into a single prompt
(``conversation_context_text``) inherits the session-level history budget.
That budget is planned against the model's *advertised* window, and the
stage's own output ``max_tokens`` is charged against the same window:

* an un-annotated model name resolves to the 16_384 fallback, while
  ``math_animator`` asks for 16_834 output tokens, so the request is
  rejected before the model sees it;
* a large-window model admits up to 131k tokens of verbatim chat into a
  prompt whose real answer is a few hundred tokens of JSON;
* a proxy or alias route can advertise more capacity than it serves, and
  the session budget cannot see that.

This module sizes the history for the *call that consumes it*: the window
minus the output reserve minus the prompt's fixed overhead, keeping the most
recent turns, and it reports what it dropped so the log shows why. It is
capability-agnostic; ``math_animator`` is the first consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from deeptutor.services.llm.context_window import resolve_effective_context_window

logger = logging.getLogger(__name__)

#: Charged per image attachment. A picture is not measured by its characters,
#: and the session history planner already charges this same estimate, so a
#: stage that counts history must count images the same way.
IMAGE_CONTEXT_TOKEN_ESTIMATE = 4096

#: Fixed overhead of a stage's system prompt, user template and formatting.
#: Measured prompt parts are counted exactly; this covers the template the
#: stage embeds around them.
PROMPT_OVERHEAD_TOKENS = 1500

#: History is never kept above this share of the window, however large the
#: headroom. A stage whose answer is a small structured payload does not need
#: 100k tokens of chat, and the session planner uses the same share.
MAX_WINDOW_RATIO = 0.35

#: Share of the remaining headroom the history may take, after the output
#: reserve and the prompt's own cost are subtracted from the window.
HEADROOM_RATIO = 0.8

#: The smallest history worth keeping. Below this the stage runs on the
#: current request alone, which is what a fresh session does.
MIN_HISTORY_TOKENS = 256

#: Output reserve floor. A reasoning model pays for its hidden thinking out
#: of the same budget as its answer, and starving it is the failure #1547
#: documents; clamping a reserve must not recreate that.
MIN_OUTPUT_TOKENS = 1024

#: What the model sees where the trimmed turns used to be. It has to say both
#: that content is missing and why, so the model does not treat the gap as a
#: topic change.
TRIM_MARKER = "[Earlier conversation omitted to fit the model's context window]"


def count_text_tokens(text: str) -> int:
    """Token estimate shared with session history budgeting.

    Imported lazily: ``services.session.context_builder`` pulls in the agent
    base classes, so a module-level import here would close an import cycle
    between the agent layer and the session layer.
    """
    from deeptutor.services.session.context_builder import count_tokens

    return count_tokens(text or "")


def attachment_tokens(attachments: Any) -> int:
    """Charge image attachments against the stage's input budget."""
    return sum(
        IMAGE_CONTEXT_TOKEN_ESTIMATE
        for item in attachments or []
        if str(getattr(item, "type", "") or "") == "image"
    )


@dataclass(frozen=True)
class StageBudget:
    """What one stage call can afford: the window, and how it is split."""

    window: int
    history_budget: int
    output_tokens: int


def plan_stage_budget(
    *,
    model: str,
    context_window: Any = None,
    output_tokens: int,
    prompt_tokens: int,
    image_tokens: int = 0,
) -> StageBudget:
    """Split one model window for one stage call.

    The output reserve is taken first, because the provider rejects a request
    whose input plus requested output exceed the window: a stage asking for
    more output than the window leaves after its prompt is not a large
    request, it is an impossible one. The history then takes the rest, capped
    by :data:`MAX_WINDOW_RATIO` so a healthy window does not invite a
    100k-token prompt for a small JSON answer.
    """
    window = resolve_effective_context_window(
        context_window=context_window,
        model=str(model or ""),
        max_tokens=output_tokens,
    )
    reserve = min(
        max(0, int(output_tokens)),
        window - int(prompt_tokens) - int(image_tokens) - MIN_HISTORY_TOKENS,
    )
    reserve = max(MIN_OUTPUT_TOKENS, min(reserve, window))
    headroom = window - reserve - int(prompt_tokens) - int(image_tokens)
    history_budget = min(int(headroom * HEADROOM_RATIO), int(window * MAX_WINDOW_RATIO))
    history_budget = max(MIN_HISTORY_TOKENS, history_budget)
    return StageBudget(window=window, history_budget=history_budget, output_tokens=reserve)


@dataclass(frozen=True)
class HistoryFit:
    """The history a stage should send, and what trimming removed."""

    text: str
    budget: int
    dropped_blocks: int
    dropped_tokens: int


def fit_history_context(history_text: str, budget: int) -> HistoryFit:
    """Trim history text to ``budget`` tokens, keeping the most recent turns.

    The history arrives as chronological blocks separated by blank lines, with
    an optional leading ``Conversation summary:`` block. The summary is the
    cheapest record of the session's arc, so it is kept first; the remaining
    budget is spent from the newest block backwards, because a follow-up
    request is answered by what was just said.
    """
    text = (history_text or "").strip()
    if not text:
        return HistoryFit(text="", budget=budget, dropped_blocks=0, dropped_tokens=0)
    total = count_text_tokens(text)
    if total <= budget:
        return HistoryFit(text=text, budget=budget, dropped_blocks=0, dropped_tokens=0)

    blocks = [block for block in text.split("\n\n") if block.strip()]
    summary = ""
    body = blocks
    if blocks and blocks[0].startswith("Conversation summary:"):
        summary = blocks[0]
        body = blocks[1:]

    kept_summary = summary if summary and count_text_tokens(summary) <= budget else ""
    kept: list[str] = []
    kept_tokens = count_text_tokens(kept_summary)
    for block in reversed(body):
        cost = count_text_tokens(block)
        if kept_tokens + cost > budget:
            break
        kept.append(block)
        kept_tokens += cost

    dropped_blocks = (len(body) - len(kept)) + (1 if summary and not kept_summary else 0)
    # A budget that cannot hold the newest turn leaves the stage with the
    # marker alone: the stage runs on the current request, which is what a
    # fresh session does, and the log records the drop.
    marker = f"[{dropped_blocks} earlier turn(s) omitted to fit the model's context window]"
    parts = [part for part in (kept_summary, marker, *reversed(kept)) if part]
    return HistoryFit(
        text="\n\n".join(parts),
        budget=budget,
        dropped_blocks=dropped_blocks,
        dropped_tokens=total - kept_tokens,
    )


__all__ = [
    "attachment_tokens",
    "count_text_tokens",
    "fit_history_context",
    "HistoryFit",
    "plan_stage_budget",
    "StageBudget",
    "PROMPT_OVERHEAD_TOKENS",
    "MIN_OUTPUT_TOKENS",
    "MIN_HISTORY_TOKENS",
    "IMAGE_CONTEXT_TOKEN_ESTIMATE",
]
