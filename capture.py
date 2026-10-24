"""Observe adapters and known direct callers without changing their results."""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import time
from contextvars import ContextVar

import aiohttp

from .accounting import (
    StreamUsage,
    caller_metadata,
    event_metadata,
    receipt,
    response_usage,
    usage_from_raw,
)
from .pricing import api_host

BOUND = ContextVar("usage_insight_bound", default=None)
DEPTH = ContextVar("usage_insight_depth", default=0)


def metadata():
    found = caller_metadata()
    bound = BOUND.get()
    if found["kind"] == "decision" and not found.get("actor"):
        if bound is not None and bound.get("active", True):
            if not found.get("origin") or found["origin"] == bound.get("origin"):
                return {
                    **{k: v for k, v in bound.items() if k != "active"},
                    "kind": "decision",
                }
    if found["kind"] != "other" or found.get("actor"):
        return found
    if bound is not None and bound.get("active", True):
        return {k: v for k, v in bound.items() if k != "active"}
    return found


class Collector:
    def __init__(self, ledger, enabled, logger, price_snapshot=None):
        self.ledger, self.enabled, self.logger = ledger, enabled, logger
        self.patches = []
        self.stopped, self.inflight = False, 0
        self.price_snapshot = price_snapshot
        self.balance_scope = None

    def bind_event(self, event):
        previous = event.get_extra("usage_insight_scope")
        if previous is None:
            previous = {**event_metadata(event), "active": True}
            event.set_extra("usage_insight_scope", previous)
            cleanup = event.cleanup_temporary_local_files

            def finish():
                previous["active"] = False
                return cleanup()

            event.cleanup_temporary_local_files = finish
        else:
            previous.update(event_metadata(event))
        BOUND.set(previous)

    def write(self, row):
        try:
            if self.price_snapshot is not None:
                snapshot = self.price_snapshot(row)
                if snapshot is not None:
                    row = {**row, "price_snapshot": snapshot}
            self.ledger.put(row)
        except Exception as exc:
            self.logger.warning("模型用量回执保存失败：%s", type(exc).__name__)

    def _patch(self, owner, name, wrapper):
        original = getattr(owner, name)
        if getattr(original, "_usage_insight_owner", None) is self:
            return
        wrapper._usage_insight_owner = self
        self.patches.append((owner, name, original, wrapper))
        setattr(owner, name, wrapper)

    def _done(self):
        self.inflight -= 1
        if self.stopped and not self.inflight:
            self.ledger.close()

    def refresh(self, providers):
        for provider in providers:
            for cls in type(provider).__mro__:
                if cls is not object:
                    for name in ("text_chat", "text_chat_stream"):
                        if name in cls.__dict__:
                            self.wrap_provider(cls, name)
        original = aiohttp.ClientSession._request
        if getattr(original, "_usage_insight_owner", None) is not self:
            self.wrap_http(original)
        # Known integrations use wait_for/background tasks, where caller frames
        # alone cannot reliably identify the source. Bind explicit local scopes.
        for module_name, module in list(sys.modules.items()):
            if module is None:
                continue
            if "astrbot_plugin_reply_gate" in module_name:
                fn = vars(module).get("judge_image")
                if (
                    callable(fn)
                    and getattr(fn, "_usage_insight_owner", None) is not self
                ):
                    self.wrap_scoped_function(
                        module, "judge_image", "decision", event_index=1
                    )
            if "astrbot_plugin_meme_library" in module_name:
                fn = vars(module).get("recognize_codex")
                if (
                    callable(fn)
                    and getattr(fn, "_usage_insight_owner", None) is not self
                ):
                    self.wrap_codex(module, fn)
                cls = vars(module).get("MemeLibrary")
                if inspect.isclass(cls) and hasattr(cls, "_provider"):
                    self.wrap_resolver(cls)
            if "astrbot_plugin_self_prompt" in module_name:
                cls = vars(module).get("SelfPrompt")
                if inspect.isclass(cls) and hasattr(cls, "_summarize"):
                    fn = getattr(cls, "_summarize")
                    if getattr(fn, "_usage_insight_owner", None) is not self:
                        self.wrap_scoped_function(
                            cls, "_summarize", "memory", scope_index=1
                        )

    def wrap_provider(self, cls, name):
        original = getattr(cls, name)
        if getattr(original, "_usage_insight_owner", None) is self or getattr(
            original, "__isabstractmethod__", False
        ):
            return
        collector = self

        @functools.wraps(original)
        async def regular(provider, *args, **kwargs):
            if collector.stopped or not collector.enabled() or DEPTH.get():
                return await original(provider, *args, **kwargs)
            start, meta, token = time.time(), metadata(), DEPTH.set(1)
            balance_ref = (
                collector.balance_scope(provider) if collector.balance_scope else ""
            )
            collector.inflight += 1
            usage, status, error = None, "completed", ""
            try:
                result = await original(provider, *args, **kwargs)
                usage = response_usage(result)
                if getattr(result, "role", "") == "err":
                    status = "error"
                return result
            except BaseException as exc:
                status, error = (
                    (
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "error"
                    ),
                    type(exc).__name__,
                )
                raise
            finally:
                DEPTH.reset(token)
                if collector.balance_scope:
                    meta = {
                        **meta,
                        "balance_ref": balance_ref
                        if balance_ref == collector.balance_scope(provider)
                        else "",
                    }
                collector.write(
                    receipt(
                        meta,
                        provider.provider_config.get("id", "unknown"),
                        kwargs.get("model") or provider.get_model(),
                        start,
                        usage,
                        status,
                        error,
                        api_host=api_host(provider.provider_config),
                    )
                )
                collector._done()

        @functools.wraps(original)
        async def streaming(provider, *args, **kwargs):
            if collector.stopped or not collector.enabled() or DEPTH.get():
                async for item in original(provider, *args, **kwargs):
                    yield item
                return
            start, meta, token = time.time(), metadata(), DEPTH.set(1)
            balance_ref = (
                collector.balance_scope(provider) if collector.balance_scope else ""
            )
            collector.inflight += 1
            usage, status, error = StreamUsage(), "completed", ""
            try:
                async for item in original(provider, *args, **kwargs):
                    usage.add(item)
                    if getattr(item, "role", "") == "err":
                        status = "error"
                    yield item
            except BaseException as exc:
                status, error = (
                    (
                        "cancelled"
                        if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                        else "error"
                    ),
                    type(exc).__name__,
                )
                raise
            finally:
                DEPTH.reset(token)
                if collector.balance_scope:
                    meta = {
                        **meta,
                        "balance_ref": balance_ref
                        if balance_ref == collector.balance_scope(provider)
                        else "",
                    }
                collector.write(
                    receipt(
                        meta,
                        provider.provider_config.get("id", "unknown"),
                        kwargs.get("model") or provider.get_model(),
                        start,
                        usage.result(),
                        status,
                        error,
                        api_host=api_host(provider.provider_config),
                    )
                )
                collector._done()

        if name == "text_chat_stream":
            self._patch(cls, name, streaming)
        else:

            @functools.wraps(original)
            def capture_call(provider, *args, **kwargs):
                # Capture LCM's worker before wait_for moves execution to a child task.
                if collector.stopped or not collector.enabled() or DEPTH.get():
                    return regular(provider, *args, **kwargs)
                found = caller_metadata()
                if found["kind"] != "compression":
                    return regular(provider, *args, **kwargs)

                async def compression_call():
                    token = BOUND.set(found)
                    try:
                        return await regular(provider, *args, **kwargs)
                    finally:
                        BOUND.reset(token)

                return compression_call()

            marker = getattr(inspect, "markcoroutinefunction", None)
            if marker is not None:
                marker(capture_call)
            self._patch(cls, name, capture_call)

    def wrap_http(self, original):
        collector = self

        @functools.wraps(original)
        async def request(session, method, url, **kwargs):
            payload = kwargs.get("json")
            if (
                collector.stopped
                or not collector.enabled()
                or not isinstance(payload, dict)
                or "questions" not in payload
                or "state" not in payload
            ):
                return await original(session, method, url, **kwargs)
            meta = metadata()
            if meta["kind"] != "decision":
                return await original(session, method, url, **kwargs)
            start, usage, status, error = time.time(), None, "completed", ""
            collector.inflight += 1
            try:
                response = await original(session, method, url, **kwargs)
                status = "completed" if 200 <= response.status < 300 else "error"
                if status == "completed":
                    try:
                        # aiohttp caches body bytes; the business caller receives
                        # the same response and reads its normal JSON afterwards.
                        body = await response.json(content_type=None)
                        usage = usage_from_raw(body)
                    except (ValueError, UnicodeError):
                        pass
                return response
            except BaseException as exc:
                status, error = (
                    (
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "error"
                    ),
                    type(exc).__name__,
                )
                raise
            finally:
                collector.write(
                    receipt(
                        meta,
                        "bailian-decision",
                        payload.get("model", "decision-model-preview"),
                        start,
                        usage,
                        status,
                        error,
                    )
                )
                collector._done()

        self._patch(aiohttp.ClientSession, "_request", request)

    def wrap_scoped_function(
        self, owner, name, kind, event_index=None, scope_index=None
    ):
        original = getattr(owner, name)

        @functools.wraps(original)
        async def scoped(*args, **kwargs):
            meta = metadata()
            if event_index is not None:
                event = (
                    args[event_index]
                    if len(args) > event_index
                    else kwargs.get("event")
                )
                if event is not None:
                    meta = event_metadata(event, kind)
            elif scope_index is not None:
                scope = (
                    args[scope_index]
                    if len(args) > scope_index
                    else kwargs.get("scope", {})
                )
                meta = {
                    "kind": kind,
                    "origin": str(scope.get("origin", "")),
                    "actor": None,
                    "participants": [],
                }
            token = BOUND.set({**meta, "kind": kind})
            try:
                return await original(*args, **kwargs)
            finally:
                BOUND.reset(token)

        self._patch(owner, name, scoped)

    def wrap_resolver(self, cls):
        original = cls._provider
        if getattr(original, "_usage_insight_owner", None) is self:
            return

        @functools.wraps(original)
        async def resolve(instance, origin, *args, **kwargs):
            result = await original(instance, origin, *args, **kwargs)
            found = caller_metadata()
            if found["kind"] == "recognition":
                BOUND.set(
                    {
                        "kind": "recognition",
                        "origin": str(origin),
                        "actor": None,
                        "participants": [],
                    }
                )
            return result

        self._patch(cls, "_provider", resolve)

    def wrap_codex(self, module, original):
        collector = self

        @functools.wraps(original)
        async def recognize(*args, **kwargs):
            if collector.stopped or not collector.enabled():
                return await original(*args, **kwargs)
            meta, start, status, error = metadata(), time.time(), "completed", ""
            meta["kind"] = "recognition"
            collector.inflight += 1
            try:
                return await original(*args, **kwargs)
            except BaseException as exc:
                status, error = "error", type(exc).__name__
                raise
            finally:
                collector.write(
                    receipt(
                        meta, "codex-cli", "gpt-6-astra", start, None, status, error
                    )
                )
                collector._done()

        self._patch(module, "recognize_codex", recognize)

    def stop(self):
        self.stopped = True
        for owner, name, original, wrapper in reversed(self.patches):
            if getattr(owner, name) is wrapper:
                setattr(owner, name, original)
        self.patches.clear()
        if not self.inflight:
            self.ledger.close()
