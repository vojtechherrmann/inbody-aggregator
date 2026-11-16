"""Result sheet (PDF / JPG / PNG) -> Claude (claude-opus-5-5) answer, under a HARD spend limit.

The interface is `call_claude(content, mime, ...)` -> JSON payload; WHAT is extracted
(prompt, JSON schema, validation) lives in `sheet_schema.py`. This module owns HOW: the
request, the budget and the accounting.

Spend control, in order, for every call:
1. `count_tokens` (free) on the exact request -> input tokens; a sheet above MAX_INPUT_TOKENS is
   rejected, never truncated.
2. Worst case = input x input price + MAX_TOKENS x output price, PLUS the same again at the
   fallback model's prices (a refusal fallback can bill both attempts). It must fit into BOTH
   the global monthly remainder and this user's remainder -- base monthly quota + active extra
   grants (quota.py), from the recorded actual cost. Otherwise: `BudgetExceeded` ->
   `skipped_budget`, no call.
3. The call itself: non-streaming, max_tokens capped; SDK retries OFF -- at most one retry
   (API error or invalid answer), and the retry goes through step 2 again.
4. Every response's actual usage is recorded per attempt (`usage.iterations`, one row per model
   that ran) BEFORE its content is interpreted, so a refusal or a malformed answer is still paid
   for in the books -- and charged to the user's base quota first, then to extra grants.
One worker thread processes uploads sequentially, so two estimates never race for the same
remainder. (Caveat: a client-side timeout after the server already finished could bill without
a response to record; API_TIMEOUT_SECONDS is generous to make that unlikely.)

Also set a monthly spend limit on the API key's workspace in the Anthropic Console -- that is the
backstop if this code is ever wrong.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

import config
import db
import quota
import sheet_schema


class BudgetExceeded(Exception):
    """`.kind`: quota_exhausted (this user's base + extra) | global_budget (the $50 cap)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class NotInBodySheet(Exception):
    """The model looked and says this is not an InBody result -> failed + CSV tip, no retry."""


class ExtractionFailed(Exception):
    """`str(exc)` is a short, user-visible reason (no secrets, no stack)."""


# ------------------------------------------------------------------------------------- pricing


def _prices(model: str) -> tuple[float, float, float, float]:
    if model in config.PRICES_PER_MTOK:
        return config.PRICES_PER_MTOK[model]
    return max(config.PRICES_PER_MTOK.values())  # unknown model: assume the dearest row


def cost_microusd(model: str, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> int:
    p_in, p_out, p_cw, p_cr = _prices(model)
    # $/MTok x tokens = micro-dollars; round UP so the books never under-count.
    total = p_in * inp + p_out * out + p_cw * cache_write + p_cr * cache_read
    return int(-(-total // 1))


def worst_case_microusd(input_tokens: int) -> int:
    worst = cost_microusd(config.MODEL, input_tokens, config.MAX_TOKENS)
    if config.FALLBACK_MODEL:
        worst += cost_microusd(config.FALLBACK_MODEL, input_tokens, config.MAX_TOKENS)
    return worst


def check_budget(conn, user_id: str, worst: int, now: datetime) -> None:
    """The worst case must fit BOTH the global monthly remainder and this user's remainder (base
    quota + active extra grants, quota.py). Raises BudgetExceeded(kind) otherwise."""
    st = quota.standing(conn, user_id, now)
    if worst > st.global_left:
        raise BudgetExceeded(
            "global_budget", f"worst case {worst} > global remaining {st.global_left}"
        )
    if worst > st.user_left:
        raise BudgetExceeded(
            "quota_exhausted",
            f"worst case {worst} > user remaining {st.user_left} "
            f"(base {st.base_left}, extra {st.extra_left})",
        )


def usage_rows(response) -> list[dict]:
    """One row per attempt that ran (primary + any fallback), from `usage.iterations`."""
    usage = response.usage
    iterations = getattr(usage, "iterations", None) or []
    attempts = [it for it in iterations if getattr(it, "input_tokens", None) is not None]
    if not attempts:
        attempts = [usage]
    rows = []
    for it in attempts:
        model = getattr(it, "model", None) or response.model
        inp = it.input_tokens or 0
        out = it.output_tokens or 0
        cw = getattr(it, "cache_creation_input_tokens", 0) or 0
        cr = getattr(it, "cache_read_input_tokens", 0) or 0
        rows.append(
            {
                "model": model,
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
                "cost_microusd": cost_microusd(model, inp, out, cw, cr),
            }
        )
    return rows


def record_usage(conn, user_id: str, upload_id: str, response) -> int:
    total = 0
    for row in usage_rows(response):
        usage_id, now = str(uuid.uuid4()), db.utcnow()
        conn.execute(
            db.api_usage.insert().values(
                id=usage_id,
                user_id=user_id,
                upload_id=upload_id,
                request_id=getattr(response, "_request_id", None),
                stop_reason=response.stop_reason,
                created_at=now,
                **row,
            )
        )
        quota.allocate(conn, user_id, usage_id, row["cost_microusd"], now)  # base, then extra
        total += row["cost_microusd"]
    return total


# ------------------------------------------------------------------------------------ extraction


SHEET_MIME_TYPES = ("application/pdf", "image/jpeg", "image/png")


def _source_block(content: bytes, mime: str) -> dict:
    encoded = base64.standard_b64encode(content).decode("ascii")
    block_type = "document" if mime == "application/pdf" else "image"
    return {"type": block_type, "source": {"type": "base64", "media_type": mime, "data": encoded}}


def _request(content: bytes, mime: str) -> dict:
    return {
        "model": config.MODEL,
        # Byte-stable prompt (no dates, no user data) -> the prefix caches (5-min TTL).
        "system": [
            {
                "type": "text",
                "text": sheet_schema.SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    _source_block(content, mime),
                    {"type": "text", "text": sheet_schema.USER_INSTRUCTION},
                ],
            }
        ],
        "output_config": {
            "effort": config.EFFORT,
            "format": {"type": "json_schema", "schema": sheet_schema.OUTPUT_SCHEMA},
        },
    }


def call_claude(content: bytes, mime: str, *, user_id: str, upload_id: str, client=None) -> dict:
    """One sheet (PDF / JPG / PNG) -> Claude's validated JSON answer (the cacheable part).

    docs/upload-pipeline.md section 2: budget check -> call -> valid JSON and an InBody sheet?
    An API error or an invalid answer is retried at most API_ATTEMPTS-1 times, and EVERY attempt
    passes the budget check again. Raises `BudgetExceeded` (that attempt made no call),
    `NotInBodySheet` (no retry) or `ExtractionFailed` (attempts exhausted).
    Turning the answer into rows is `sheet_schema.to_extracted` -- free to re-run."""
    import anthropic

    if mime not in SHEET_MIME_TYPES:
        raise ExtractionFailed("unsupported sheet type")
    client = client or make_client()
    request = _request(content, mime)
    counted = client.messages.count_tokens(**request).input_tokens
    if counted > config.MAX_INPUT_TOKENS:
        raise ExtractionFailed("document too large")
    extra = {}
    if config.FALLBACK_MODEL:
        extra = {"betas": [config.FALLBACK_BETA], "fallbacks": [{"model": config.FALLBACK_MODEL}]}

    last_error = "no attempt made"
    for _attempt in range(config.API_ATTEMPTS):
        with db.engine().begin() as conn:
            check_budget(conn, user_id, worst_case_microusd(counted), db.utcnow())
        try:
            response = client.beta.messages.create(max_tokens=config.MAX_TOKENS, **request, **extra)
        except anthropic.APIError as exc:  # status / connection / timeout errors
            last_error = f"API error ({type(exc).__name__})"
            continue
        with db.engine().begin() as conn:
            record_usage(conn, user_id, upload_id, response)
        if response.stop_reason == "refusal":
            raise ExtractionFailed(
                "the model declined this document"
            )  # retrying would only pay again
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            payload = json.loads(text or "")
            sheet_schema.validate_wire(payload)  # raises NotInBodySheet / SheetInvalid
        except sheet_schema.NotInBodySheet as exc:
            raise NotInBodySheet(str(exc)) from exc
        except (ValueError, sheet_schema.SheetInvalid):
            last_error = (
                "output limit reached"
                if response.stop_reason == "max_tokens"
                else "invalid model response"
            )
            continue
        return payload
    raise ExtractionFailed(last_error)


def make_client():
    import anthropic

    # max_retries=0: the ONLY retry is the budget-checked loop in call_claude.
    return anthropic.Anthropic(max_retries=0, timeout=config.API_TIMEOUT_SECONDS)
