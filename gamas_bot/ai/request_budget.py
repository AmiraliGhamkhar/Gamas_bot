"""Request-context preflight, distinct from the provider's rate/quota ledger.

Token counts are conservative estimates, not a promise about a remote tokenizer.
The *built* request is counted, including its schema and transport framing. No
credential, prompt, or response body is retained in a BudgetDecision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .tokens import estimate_tokens

if TYPE_CHECKING:
    from .adapters import NoteRequest
    from .models import ModelInfo

# An unpublished/custom gateway has no verified window. Bound it rather than
# treating zero as infinite. Administrators can supply verified model metadata.
UNKNOWN_CONTEXT_TOKENS = 16_384
UNKNOWN_OUTPUT_TOKENS = 8_192
REQUEST_SAFETY_MARGIN = 0.15


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    input_tokens: int
    output_tokens: int
    input_limit: int
    context_limit: int
    document_token_budget: int | None
    reason: str = ""  # safe enum; never include request content

    @property
    def fits(self) -> bool:
        return not self.reason


def check_request_budget(
    request: NoteRequest,
    model: ModelInfo,
    *,
    output_tokens: int,
    document_token_budget: int | None = None,
    margin: float = REQUEST_SAFETY_MARGIN,
    input_only_window: bool = False,
) -> BudgetDecision:
    """Check the selected model's actual request BEFORE quota reservation/HTTP.

    ``context_window`` denotes an input-only window for Gemini's native API;
    other supported chat transports advertise a combined input/output window.
    A missing context or output limit receives a finite conservative ceiling.
    Profile document budgets drive the initial source splitter, while this
    check governs the complete serialized request. Rate limits are separate.
    """
    context = model.context_window if model.context_window > 0 else UNKNOWN_CONTEXT_TOKENS
    output_limit = model.max_output_tokens if model.max_output_tokens > 0 else UNKNOWN_OUTPUT_TOKENS
    output = max(1, int(output_tokens))
    # JSON serialization counts system/user messages, schemas and API framing;
    # ensure_ascii=False avoids inflating the Persian text with escape sequences.
    serialized = json.dumps(request.json_body, ensure_ascii=False, separators=(",", ":"))
    estimated = estimate_tokens(serialized) + 32  # protocol/message framing
    safety = max(0.0, min(float(margin), 0.5))
    usable = int(context * (1.0 - safety))
    input_limit = usable if input_only_window else max(0, usable - output)
    if output > output_limit:
        reason = "output_limit"
    elif input_limit <= 0:
        reason = "instructions_or_output_exceed_context"
    elif estimated > input_limit:
        reason = "input_exceeds_context"
    else:
        reason = ""
    return BudgetDecision(
        input_tokens=estimated,
        output_tokens=output,
        input_limit=input_limit,
        context_limit=context,
        document_token_budget=document_token_budget,
        reason=reason,
    )
