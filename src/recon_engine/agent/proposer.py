"""
Takes an investigator's free-text findings and forces them into a
validated ResolutionProposal.

Originally called the model through Guardrails AI's own Guard.for_pydantic
mechanism. That worked reliably against Gemini but proved flaky against
Ollama specifically -- roughly 75% of calls came back with completely
empty output (raw_llm_output=''), while our own hand-written litellm call
in investigator.py succeeded 8/8 times across two separate runs on the
same model. Rather than keep patching prompts around a third-party
integration bug, this calls litellm directly (the proven-reliable path)
and does the schema validation + retry-on-failure ourselves -- same
Guardrails *principle* (nothing gets trusted without passing Pydantic
validation), different mechanism.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Literal

import litellm
from pydantic import BaseModel, Field, ValidationError

PROPOSER_MODEL = os.environ.get("PROPOSER_MODEL", "gemini/gemini-3.1-flash-lite")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
MAX_ATTEMPTS = 3
MAX_RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BASE_DELAY_SECONDS = 20  # Gemini free tier's own errors suggest ~17-18s

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache" / "proposals"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

RESOLUTION_TYPES = Literal[
    "confirmed_match", "confirmed_batch", "confirmed_orphan",
    "flagged_for_manual_review", "insufficient_evidence",
]


class ResolutionProposal(BaseModel):
    internal_txn_id: str = Field(description="The internal transaction ID being resolved")
    resolution_type: RESOLUTION_TYPES = Field(description="The investigator's conclusion")
    matched_settlement_line_ids: list[str] = Field(
        default_factory=list,
        description="Settlement line ID(s) this transaction resolves to, if any",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this resolution, 0-1")
    reasoning: str = Field(description="Brief explanation citing specific evidence gathered")
    requires_human_approval: bool = Field(
        description="True if confidence < 0.85 or resolution_type involves a dollar write-off/adjustment"
    )


class _LLMResolutionOutput(BaseModel):
    """What we actually ask the LLM to generate -- deliberately excludes
    internal_txn_id. We already know that value when we call
    propose_resolution(); asking the model to echo an ID back verbatim is
    an unreliable ask (it returned null once) for something that doesn't
    need generating at all. We attach the real ID ourselves afterward."""
    resolution_type: RESOLUTION_TYPES = Field(description="The investigator's conclusion")
    matched_settlement_line_ids: list[str] = Field(
        default_factory=list,
        description="Settlement line ID(s) this transaction resolves to, if any",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this resolution, 0-1")
    reasoning: str = Field(description="Brief explanation citing specific evidence gathered")
    requires_human_approval: bool = Field(
        description="True if confidence < 0.85 or resolution_type involves a dollar write-off/adjustment"
    )


SCHEMA_INSTRUCTIONS = """Respond with ONLY a JSON object, no other text, matching exactly this shape:
{
  "resolution_type": one of "confirmed_match", "confirmed_batch", "confirmed_orphan", "flagged_for_manual_review", "insufficient_evidence",
  "matched_settlement_line_ids": [list of strings, empty list if none],
  "confidence": number between 0.0 and 1.0,
  "reasoning": "brief one or two sentence explanation",
  "requires_human_approval": true or false
}

IMPORTANT: matched_settlement_line_ids must contain ONLY settlement line IDs \
(they start with "SETL-"). Never put the transaction's own internal ID \
(starts with "TXN-") in this list -- an internal transaction is never its \
own settlement line. If you don't have a real SETL- id to cite, use an \
empty list."""


def _cache_path(internal_txn_id: str) -> Path:
    model_tag = hashlib.md5(PROPOSER_MODEL.encode()).hexdigest()[:8]
    return CACHE_DIR / f"{internal_txn_id}_{model_tag}.json"


def _completion_with_backoff(**kwargs):
    """The investigator has always had rate-limit/overload retry logic
    (_completion_with_retry in investigator.py); this file never did,
    which is why a batch run against Gemini's free tier produced a wall
    of immediate RateLimitError failures instead of waiting the ~17-18s
    Gemini itself suggests and trying again."""
    last_error = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            return litellm.completion(**kwargs)
        except (litellm.RateLimitError, litellm.ServiceUnavailableError) as e:
            last_error = e
            if attempt < MAX_RATE_LIMIT_RETRIES - 1:
                delay = RATE_LIMIT_BASE_DELAY_SECONDS * (attempt + 1)
                print(f"[propose] rate limited, waiting {delay}s before retry "
                      f"({attempt + 1}/{MAX_RATE_LIMIT_RETRIES})...")
                time.sleep(delay)
    raise last_error


def _extract_json(text: str) -> dict:
    """Models frequently wrap JSON in markdown fences or add stray text
    around it despite instructions not to -- find the outermost {...}
    block rather than assuming the whole response is clean JSON."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise json.JSONDecodeError("No JSON object found in response", text, 0)
    return json.loads(text[start:end + 1])


def propose_resolution(internal_txn_id: str, investigation_findings: str) -> ResolutionProposal:
    cache_file = _cache_path(internal_txn_id)
    if cache_file.exists():
        return ResolutionProposal.model_validate(json.loads(cache_file.read_text()))

    prompt_content = (
        f"An investigator produced these findings for a transaction:\n\n"
        f"{investigation_findings}\n\n"
        f"Convert this into a structured resolution proposal. "
        f"If the investigator's findings are inconclusive or contradictory, "
        f"use resolution_type='insufficient_evidence' rather than guessing. "
        f"If the findings describe two or more candidates that could not be "
        f"distinguished from each other, use resolution_type="
        f"'flagged_for_manual_review' with LOW confidence (below 0.5) and "
        f"list all the candidate settlement line IDs -- do not pick one "
        f"arbitrarily and report high confidence. "
        f"Keep 'reasoning' brief -- one or two sentences.\n\n{SCHEMA_INSTRUCTIONS}"
    )
    # NOTE: previously appended "/no_think" here (Qwen's directive to skip
    # its reasoning phase) to fix empty-output failures. That fix worked,
    # but likely introduced a worse problem: this step asks the model to
    # synthesize several thousand characters of findings into a judgment
    # call, and without a reasoning pass, a 4B model appears to default to
    # a safe "insufficient_evidence" hedge rather than actually reasoning
    # through the evidence (observed: hedged even on cases with substantial,
    # relevant findings). Our own retry loop now handles empty output
    # gracefully, so /no_think isn't needed for that anymore -- testing
    # without it, with more token headroom for a real thinking pass.
    completion_kwargs = dict(model=PROPOSER_MODEL, max_tokens=4096)
    if PROPOSER_MODEL.startswith("ollama"):
        completion_kwargs["api_base"] = OLLAMA_API_BASE

    print(f"[propose] {internal_txn_id}: sending {len(prompt_content)} char prompt "
          f"({len(investigation_findings)} chars of findings included)")

    messages = [{"role": "user", "content": prompt_content}]
    last_error = None

    for attempt in range(MAX_ATTEMPTS):
        response = _completion_with_backoff(messages=messages, **completion_kwargs)
        raw = response.choices[0].message.content or ""
        try:
            parsed = _extract_json(raw)
            llm_output = _LLMResolutionOutput.model_validate(parsed)
            proposal = ResolutionProposal(internal_txn_id=internal_txn_id, **llm_output.model_dump())
            cache_file.write_text(json.dumps(proposal.model_dump()))
            return proposal
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = f"attempt {attempt + 1}: {type(e).__name__}: {e}. raw_output={raw!r}"
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"That wasn't valid: {e}. Respond again with ONLY the JSON object, nothing else.",
            })

    raise RuntimeError(
        f"Failed to get valid structured output for {internal_txn_id} after "
        f"{MAX_ATTEMPTS} attempts. Last failure: {last_error}"
    )
