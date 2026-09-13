"""
The investigator: given one exception case, gives the model the three
MCP tools and lets it look around before saying anything. Produces
free-text findings; structure gets enforced separately in proposer.py.

Built on litellm rather than a provider SDK directly, so the model is a
one-line config change (INVESTIGATOR_MODEL below) instead of a rewrite --
this is what let the project move from Claude Haiku to Gemini's free tier
without touching the tool-calling loop itself, only the MCP-tool-schema
conversion (OpenAI-style function-calling format works across providers
via litellm; Anthropic's raw SDK format does not).

Results are cached by source snapshot, case, model and implementation content. Every debugging re-run
during development was re-paying for investigation work that had already
succeeded before failing on a later, unrelated bug -- this cache means a
re-run only pays (in time or quota) for cases it hasn't seen before.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import litellm
from mcp import Client
from recon_engine.agent.cache import digest, source_fingerprint, read as read_cache, write as write_cache

from recon_engine.mcp_server.server import mcp

# One place to change providers -- or override via env var without a code
# change. litellm's model-string prefix selects the provider.
#   Claude Haiku:            "anthropic/claude-haiku-4-5-20251001"
#   Gemini free (flash):     "gemini/gemini-3.5-flash"
#   Gemini free (flash-lite): "gemini/gemini-3.1-flash-lite" -- less demand,
#     worth trying if 3.5-flash is returning 503 overload errors.
#   Local Ollama:            "ollama_chat/qwen3:4b" -- NOT "ollama/", which
#     routes through Ollama's older /api/generate endpoint and does not
#     support tool calling. Requires `ollama serve` running locally and
#     the model already pulled (`ollama pull qwen3:4b`). Tool-calling
#     reliability with local models is genuinely less consistent than
#     hosted APIs -- test with INVESTIGATE_LIMIT=1 before a full run.
INVESTIGATOR_MODEL = os.environ.get("INVESTIGATOR_MODEL", "gemini/gemini-3.1-flash-lite")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
MAX_TOOL_ROUNDS = 6
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2
# Local models can be slow on a cold start (loading weights into memory)
# but should never hang indefinitely. 300s (up from 120s) to match the
# max_tokens=4096 generation budget -- a full-length response legitimately
# takes longer to generate than the truncated 1024-token version did.
REQUEST_TIMEOUT_SECONDS = 300

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache" / "investigations"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(case: dict, source: str) -> Path:
    implementation = [Path(__file__).read_text(),
                      (Path(__file__).parents[1] / "mcp_server/server.py").read_text()]
    return CACHE_DIR / (digest([case, source, INVESTIGATOR_MODEL, OLLAMA_API_BASE,
                               implementation]) + ".json")


def _mcp_tool_to_openai_schema(tool) -> dict:
    """litellm normalizes tool-calling to OpenAI's function-calling shape
    across providers (Anthropic, Gemini, etc.) -- this is what makes the
    investigator provider-agnostic."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


async def _completion_with_retry(**kwargs):
    """Retries on transient failures (503 overload, rate limits) with
    exponential backoff. Does NOT retry on permission/auth errors (403,
    401) -- those won't succeed on retry, and retrying them just burns
    time and quota for no reason."""
    if kwargs.get("model", "").startswith("ollama"):
        kwargs.setdefault("api_base", OLLAMA_API_BASE)

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return await asyncio.wait_for(
                litellm.acompletion(**kwargs), timeout=REQUEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as e:
            last_error = RuntimeError(
                f"Request timed out after {REQUEST_TIMEOUT_SECONDS}s. "
                f"If using Ollama, the model may still be loading into memory "
                f"on first use (try again), or the model may be too large/slow "
                f"for real-time use on this hardware."
            )
            break  # don't retry a timeout -- if it's genuinely stuck, retrying won't help
        except (litellm.ServiceUnavailableError, litellm.RateLimitError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                await asyncio.sleep(delay)
    raise last_error


INVESTIGATOR_SYSTEM_PROMPT = """You are a reconciliation investigator for an ACH/card \
settlement system. You'll be given one exception case that the deterministic \
matcher could not resolve. Use the available tools to gather evidence before \
concluding anything -- do not guess.

IMPORTANT: check the case's evidence_so_far field first. If it contains
"matcher_candidate_settlement_ids", the deterministic matcher has ALREADY
identified specific settlement line(s) it considers relevant to this case
(e.g., two lines it couldn't distinguish between for a duplicate). You MUST
call get_batch_context on those specific settlement line ID(s) before doing
anything else -- investigating the internal transaction history instead of
these flagged lines will miss the actual evidence the matcher already found.

CRITICAL -- do not manufacture false certainty on genuinely ambiguous cases:
if there are two (or more) candidate settlement lines with the SAME amount,
date, and account, and nothing in your investigation (batch_id continuity,
transaction history, descriptor differences) distinguishes one as more
likely than the other, you have NOT resolved this case. Concluding
"confirmed_match" and picking one arbitrarily is worse than admitting
uncertainty -- state clearly that the candidates are indistinguishable and
this needs human judgment, with LOW confidence, not high confidence on a
coin flip.

Specifically:
- If a batch match was proposed, use get_batch_context to check whether the \
settlement line's batch_id is shared with other lines. Real batches show \
batch_id continuity; a coincidental amount match will not.
- Use lookup_transaction_history to understand whether this account transacts \
frequently (raising the risk that an amount-only match is a false positive, \
like two identical purchases on the same day).
- Use search_settlement_lines if you need to look beyond the case's own account \
or original date window.
- If NO settlement line matches this transaction alone (no candidate found
by account_last4 or amount), use search_batch_candidates BEFORE concluding
this is an orphan -- it searches for combinations of other transactions
that might sum to match a settlement line together with this one.
search_batch_candidates CANNOT truly verify a batch (this dataset has no
shared identifier that would allow that -- a deliberate, documented
limitation, not a bug). A "found" result means only that amounts summed
correctly -- report it as an unverified candidate lead with confidence no
higher than ~0.6, never as "confirmed_batch" with high confidence. A
"found=False" result IS reliable exhaustive evidence, and safely supports
"confirmed_orphan" with high confidence. Only conclude "confirmed_orphan"
after search_batch_candidates comes back found=False -- guessing orphan
without trying this tool first is exactly the mistake that made this
pipeline's first evaluation run score 30% accuracy, almost entirely on
missed batches.

When you're done investigating, write a concise plain-text summary of what you \
found and what you believe the correct resolution is, with your reasoning and \
your confidence. Do not format this as JSON -- just write it clearly, another \
step will structure it afterward."""


async def investigate_case(case: dict) -> str:
    """Runs the tool-calling loop for one exception case. Returns the
    model's free-text investigation summary. Cached to disk by
    internal_txn_id + model, so re-running the pipeline doesn't re-pay
    for cases already investigated."""
    source = source_fingerprint()
    cache_file = _cache_path(case, source)
    cached_data = read_cache(cache_file)
    if isinstance(cached_data.get("findings"), str) and cached_data.get("tool_calls_made", 0) > 0:
        return cached_data["findings"]

    async with Client(mcp) as mcp_client:
        tool_list = await mcp_client.list_tools()
        openai_tools = [_mcp_tool_to_openai_schema(t) for t in tool_list.tools]

        user_content = (
            f"Exception case:\n"
            f"  internal_txn_id: {case['internal_txn_id']}\n"
            f"  transaction_date: {case['transaction_date']}\n"
            f"  amount: {case['amount']}\n"
            f"  account_last4: {case['account_last4']}\n"
            f"  merchant_descriptor: {case['merchant_descriptor']}\n"
            f"  original_match_type: {case['original_match_type']}\n"
            f"  rule_based_classification: {case['classification']}\n"
            f"  evidence_so_far: {case['evidence']}\n"
        )
        messages = [
            {"role": "system", "content": INVESTIGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        findings = None
        tool_calls_made = 0
        for round_num in range(MAX_TOOL_ROUNDS):
            response = await _completion_with_retry(
                model=INVESTIGATOR_MODEL,
                max_tokens=4096,  # was 1024 -- too small for qwen3's thinking-mode
                                  # overhead, which was silently truncating responses
                                  # mid-thought before any real tool call happened
                tools=openai_tools,
                messages=messages,
            )
            choice = response.choices[0]
            message = choice.message

            if choice.finish_reason == "length":
                # The response was cut off by the token limit, not a genuine
                # conclusion. Accepting truncated content here was the actual
                # root cause of every "successful" investigation that turned
                # out to be unfinished chain-of-thought narrating an intended
                # tool call rather than a real one -- fail loudly instead.
                raise RuntimeError(
                    f"Response truncated by max_tokens on round {round_num + 1} "
                    f"(finish_reason=length) before reaching a real conclusion or "
                    f"tool call. Partial content: {(message.content or '')[-300:]!r}"
                )

            if not message.tool_calls:
                content = message.content or getattr(message, "reasoning_content", None)

                if content and tool_calls_made == 0:
                    # The model is trying to conclude without ever having made
                    # a real tool call -- this is exactly the failure mode
                    # found in this session's cache (narrating "the tool call
                    # would be {...}" in prose instead of actually calling it).
                    # Force at least one genuine tool call before any answer
                    # is accepted.
                    messages.append(message.model_dump())
                    messages.append({
                        "role": "user",
                        "content": "You haven't actually called any tools yet -- you "
                                    "only described what you might do. You must use "
                                    "the tools (not describe them) before concluding. "
                                    "Make a real tool call now.",
                    })
                    continue

                if content:
                    findings = content
                    break

                if round_num < MAX_TOOL_ROUNDS - 1:
                    messages.append(message.model_dump())
                    messages.append({
                        "role": "user",
                        "content": "You didn't provide any text. Please write your "
                                    "investigation summary now, in plain text.",
                    })
                    continue
                raise RuntimeError(
                    f"Model returned empty content with no tool call after "
                    f"{round_num + 1} rounds, even after a nudge. Last message: "
                    f"{message.model_dump()!r}"
                )

            messages.append(message.model_dump())

            for tool_call in message.tool_calls:
                tool_calls_made += 1
                args = json.loads(tool_call.function.arguments)
                result = await mcp_client.call_tool(tool_call.function.name, args)
                result_text = "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_text,
                })

        if findings is None:
            raise RuntimeError(
                f"Exceeded MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS} without a concluding answer "
                f"(tool_calls_made={tool_calls_made})."
            )

    if source_fingerprint() != source:
        raise ValueError("Source data changed during investigation; retry with a new snapshot")
    write_cache(cache_file, {"findings": findings, "tool_calls_made": tool_calls_made})
    return findings
