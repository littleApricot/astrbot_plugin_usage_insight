"""Normalize reported counters without estimating tokens from message length."""

from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass, field


def mapping(value):
    if isinstance(value, dict):
        return value
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    return vars(value) if hasattr(value, "__dict__") else {}


def counter(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return (
        int(value)
        if math.isfinite(value) and value >= 0 and value == int(value)
        else None
    )


def usage_from_raw(raw):
    usage = mapping(mapping(raw).get("usage"))
    if not usage:
        return None
    input_count = counter(usage.get("prompt_tokens", usage.get("input_tokens")))
    output = counter(usage.get("completion_tokens", usage.get("output_tokens")))
    details = mapping(
        usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    )
    cached = counter(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
    if "cache_read_input_tokens" in usage:
        cached = counter(usage["cache_read_input_tokens"])
        write = counter(usage.get("cache_creation_input_tokens", 0))
        # Cache writes may have a separate price; leave the split unknown.
        if input_count is not None and cached is not None and write is not None:
            input_count += cached + write
        if write:
            cached = None
    if input_count is None or output is None:
        return None
    if cached is not None and cached > input_count:
        cached = None
    return {"input": input_count, "cached": cached, "output": output}


def response_usage(response):
    raw = usage_from_raw(getattr(response, "raw_completion", None))
    if raw is not None:
        return raw
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    other = counter(getattr(usage, "input_other", None))
    cached = counter(getattr(usage, "input_cached", None))
    output = counter(getattr(usage, "output", None))
    if None in (other, cached, output):
        return None
    # Native default zero cannot prove that this provider reported cache usage.
    return {
        "input": other + cached,
        "cached": cached if cached > 0 else None,
        "output": output,
    }


@dataclass
class StreamUsage:
    final: dict | None = None
    chunks: list[dict] = field(default_factory=list)

    def add(self, response):
        usage = response_usage(response)
        if usage is None:
            return
        if getattr(response, "is_chunk", False):
            self.chunks.append(usage)
        else:
            self.final = usage

    def result(self):
        if self.final is not None:
            return self.final
        if not self.chunks:
            return None
        return {
            "input": sum(u["input"] for u in self.chunks),
            "output": sum(u["output"] for u in self.chunks),
            "cached": sum(u["cached"] for u in self.chunks)
            if all(u["cached"] is not None for u in self.chunks)
            else None,
        }


def event_identity(event):
    uid = str(event.get_sender_id())
    platform = str(event.get_platform_id())
    return {"id": platform + ":" + uid, "name": str(event.get_sender_name() or uid)}


def event_metadata(event, kind="reply"):
    actor = event_identity(event)
    participants = {actor["id"]: actor}
    batch = event.get_extra("reply_gate_batch")
    for member in getattr(batch, "members", []):
        person = event_identity(member.event)
        participants[person["id"]] = person
    return {
        "kind": kind,
        "origin": str(event.unified_msg_origin),
        "actor": actor,
        "participants": list(participants.values()),
    }


def caller_metadata():
    """Inspect only known integration frames; never retain frame or message data."""
    result = {"kind": "other", "origin": "", "actor": None, "participants": []}
    frame = inspect.currentframe()
    try:
        for _ in range(45):
            frame = frame.f_back if frame is not None else None
            if frame is None:
                break
            module = str(frame.f_globals.get("__name__", ""))
            values = frame.f_locals
            if "astrbot_plugin_reply_gate" in module:
                result["kind"] = "decision"
            elif (
                "astrbot_plugin_self_prompt" in module
                and frame.f_code.co_name == "_summarize"
            ):
                scope = values.get("scope", {})
                return {
                    **result,
                    "kind": "memory",
                    "origin": str(scope.get("origin", "")),
                }
            elif "astrbot_plugin_meme_library" in module and frame.f_code.co_name in (
                "_process_queue",
                "recognize_codex",
            ):
                record = values.get("record", {})
                result.update(kind="recognition", origin=str(record.get("origin", "")))
            if module.startswith(("astrbot.", "data.plugins.", "astrbot_plugin_")):
                event = values.get("event")
                if callable(getattr(event, "get_sender_id", None)):
                    kind = result["kind"] if result["kind"] != "other" else "reply"
                    return event_metadata(event, kind)
        return result
    finally:
        del frame


def receipt(
    metadata,
    provider,
    model,
    start,
    usage,
    status="completed",
    error="",
    *,
    api_host=None,
):
    return {
        **metadata,
        "provider": str(provider),
        "model": str(model),
        "ts": start,
        "duration": max(0, time.time() - start),
        "usage": usage,
        "status": status,
        "error": error,
        "unit": "call",
        **({"api_host": api_host} if api_host is not None else {}),
    }
