"""Mitigation + observability layer wrapped around the opaque agent (a REAL LLM).

The agent is silent, so this is the ONLY place telemetry can live. Here we:
  - time every call, record latency / tokens / cost / tools / steps / PII (observability),
  - sanitize injected order notes before they reach the model (injection defense),
  - cache identical questions (thread-safe) to cut cost/latency on repeats,
  - retry transient agent/tool errors with backoff,
  - redact any email/phone that still leaks into the answer (PII guard),
  - route our rewritten system prompt (prompt.txt) on every request.

Legal moves only: retry / cache / route / sanitize / redact / logging. No hardcoded
answers, no agent internals, no network.
"""
from __future__ import annotations
import os
import random
import re
import time

try:  # bundled Day-13 toolkit (optional)
    from telemetry.logger import logger
    from telemetry.cost import cost_from_usage
    from telemetry.redact import redact
except Exception:  # keep running even without the toolkit
    logger = None

    def cost_from_usage(model, usage):
        return 0.0

    def redact(s, mask="[REDACTED:{}]"):
        if not isinstance(s, str):
            return s, 0
        n = 0
        s, k = re.subn(r"[\w.+-]+@[\w-]+\.[\w.-]+", mask.format("EMAIL"), s); n += k
        s, k = re.subn(r"\b(?:\+84|0)\d{9}\b", mask.format("PHONE_VN"), s); n += k
        return s, n


# Order-note / injection markers. We cut from the marker to end-of-line so a legit
# order that precedes an appended note is preserved (the prompt is the primary defense;
# this is a backstop).
_NOTE_RE = re.compile(
    r"(?im)(ghi\s*ch[uú]|system\s*:|b[oỏ]\s*qua|ignore\b|instruction|gi[aá]\s*l[aà]|set\s+price).*$"
)
_RETRYABLE = ("error", "wrapper_error", "no_action")


def _sanitize(question: str) -> str:
    """Strip injected instruction lines from order notes; keep them as inert data."""
    if not isinstance(question, str):
        return question
    return _NOTE_RE.sub("[note]", question)


def _prompt_text(config):
    """Prefer our rewritten prompt.txt; fall back to whatever config already carries."""
    path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return config.get("system_prompt")


def mitigate(call_next, question, config, context):
    cache = context.get("cache")
    lock = context.get("cache_lock")
    key = (question or "").strip()

    # 1) cache identical questions (thread-safe)
    if cache is not None and lock is not None:
        with lock:
            if key in cache:
                return cache[key]

    # 2) input sanitize: strip injected instruction lines AND redact PII from the
    #    question itself. Email/phone are never needed to compute a total, so removing
    #    them at the source means the model can never echo them back (PII = 0 at the
    #    agent boundary, not just in our final answer).
    safe_q = redact(_sanitize(question))[0]
    conf = dict(config)
    sp = _prompt_text(config)
    if sp:
        conf["system_prompt"] = sp

    # 4) retry transient errors AND exceptions with backoff; never let mitigate() raise
    #    (an uncaught exception here is scored as wrapper_error -> a lost request).
    attempts = int((config.get("retry") or {}).get("max_attempts", 1)) or 1
    backoff = int((config.get("retry") or {}).get("backoff_ms", 0))
    t0 = time.time()
    result, last_exc = None, None
    for i in range(attempts):
        try:
            result = call_next(safe_q, conf)
            last_exc = None
            status = (result or {}).get("status")
            if status not in _RETRYABLE:
                break
        except Exception as exc:  # transient LLM/tool failure (e.g. rate limit) -> retry
            result, last_exc = None, exc
        if i < attempts - 1:
            # exponential backoff + jitter so a rate-limit burst recovers instead of
            # turning into a lost request (caps at 4s/attempt).
            base = backoff if backoff else 250
            delay = min(base * (2 ** i), 4000) + random.randint(0, 250)
            time.sleep(delay / 1000.0)
    wall_ms = int((time.time() - t0) * 1000)

    # never propagate: hand back a well-formed result so the request still counts
    if not isinstance(result, dict):
        result = {"answer": None, "status": "error", "steps": 0, "trace": [],
                  "meta": {"latency_ms": wall_ms, "usage": {}, "error": str(last_exc)}}

    # 5) PII guard on the answer
    answer = result.get("answer")
    pii_n = 0
    if isinstance(answer, str):
        answer, pii_n = redact(answer)
        result["answer"] = answer

    # 6) observability — the only telemetry we get
    meta = result.get("meta", {}) or {}
    usage = meta.get("usage", {}) or {}
    if logger:
        logger.log_event("AGENT_CALL", {
            "qid": context.get("qid"),
            "session": context.get("session_id"),
            "turn": context.get("turn_index"),
            "status": result.get("status"),
            "steps": result.get("steps"),
            "reported_latency_ms": meta.get("latency_ms"),
            "wall_ms": wall_ms,
            "usage": usage,
            "cost_usd": cost_from_usage(meta.get("model", ""), usage),
            "tools_used": meta.get("tools_used", []),
            "tool_calls": len(meta.get("tools_used", []) or []),
            "pii_redacted": pii_n,
        })

    # 7) store in cache
    if cache is not None and lock is not None and result.get("status") == "ok":
        with lock:
            cache[key] = result
    return result
