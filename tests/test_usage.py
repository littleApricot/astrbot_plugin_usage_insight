import asyncio
import copy
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.db.po import ProviderStat
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot_plugin_usage_insight.accounting import (
    StreamUsage,
    receipt,
    response_usage,
    usage_from_raw,
)
from astrbot_plugin_usage_insight.capture import BOUND, Collector
from astrbot_plugin_usage_insight.ledger import Ledger, estimate, prices_from_json
from astrbot_plugin_usage_insight.main import UsageInsight
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from openai.types.chat import ChatCompletion
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


def raw_usage(cached=60):
    return {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": cached,
        }
    }


def row(actor=None, participants=None, usage=None, **kwargs):
    person = actor or {"id": "qq:u1", "name": "甲"}
    value = receipt(
        {
            "kind": "reply",
            "origin": "qq:group:g1",
            "actor": person,
            "participants": participants or [person],
        },
        "deepseek",
        "model",
        time.time(),
        usage if usage is not None else {"input": 100, "cached": 60, "output": 20},
    )
    return {**value, **kwargs}


def rules(currency="CNY", **extra):
    return prices_from_json(
        [
            {
                **{
                    "provider": "deepseek",
                    "model": "model",
                    "currency": currency,
                    "input": "2",
                    "cached": ".2",
                    "output": "3",
                },
                **extra,
            }
        ]
    )


def test_usage_uses_reported_deepseek_cache_and_does_not_double_count_reasoning():
    assert usage_from_raw(raw_usage()) == {"input": 100, "cached": 60, "output": 20}
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 50},
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
    }
    assert usage_from_raw(raw)["output"] == 30
    assert usage_from_raw(raw)["cached"] == 50


@pytest.mark.parametrize("bad", [None, "5", True, -1, float("inf"), 1.5])
def test_invalid_usage_does_not_turn_unknown_into_zero(bad):
    assert (
        usage_from_raw({"usage": {"prompt_tokens": bad, "completion_tokens": 3}})
        is None
    )


def test_native_zero_cache_is_unknown_but_explicit_raw_zero_is_known():
    response = LLMResponse(
        role="assistant", usage=TokenUsage(input_other=100, input_cached=0, output=20)
    )
    assert response_usage(response)["cached"] is None
    response.raw_completion = raw_usage(0)
    assert response_usage(response)["cached"] == 0


def test_stream_final_usage_replaces_chunk_usage():
    stream = StreamUsage()
    stream.add(
        LLMResponse(role="assistant", is_chunk=True, usage=TokenUsage(100, 50, 10))
    )
    stream.add(LLMResponse(role="assistant", usage=TokenUsage(100, 50, 15)))
    assert stream.result() == {"input": 150, "cached": 50, "output": 15}


def test_estimated_fee_and_cache_savings():
    fee = estimate(row(), rules())
    assert fee["total"] == pytest.approx((40 * 2 + 60 * 0.2 + 20 * 3) / 1e6)
    assert fee["saving"] == pytest.approx(60 * 1.8 / 1e6)
    unknown = row(usage={"input": 100, "cached": None, "output": 20})
    assert estimate(unknown, rules()) is None
    assert estimate(unknown, rules(cached="2.0")) is not None


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        '[{"model":"x","input":-1,"cached":0,"output":1}]',
        '[{"model":"x","input":"NaN","cached":0,"output":1}]',
        '[{"model":"x","input":true,"cached":0,"output":1}]',
    ],
)
def test_bad_prices_rejected(value):
    with pytest.raises(ValueError):
        prices_from_json(value)


def test_totals_denominators_currencies_and_persistence(tmp_path):
    path = tmp_path / "usage.db"
    ledger = Ledger(path)
    ledger.put(row())
    ledger.put(
        row(usage={"input": 200, "cached": None, "output": 30}, provider="other")
    )
    ledger.put(row(usage=None) | {"usage": None, "status": "error"})
    report = ledger.report(rules() + rules(currency="USD", provider="other"), days=0)
    summary = report["summary"]
    assert summary["total"] == 350
    assert summary["cache_rate"] == 0.6
    assert summary["cache_coverage"] == pytest.approx(1 / 3)
    assert summary["priced_count"] == 1
    assert summary["usage_count"] == 2 and summary["errors"] == 1
    ledger.close()
    again = Ledger(path)
    assert again.report([], days=0)["count"] == 3
    again.close()


def test_batch_attribution_filters_share_and_shared_cost_reconcile(tmp_path):
    ledger = Ledger(tmp_path / "usage.db")
    a, b = {"id": "qq:a", "name": "甲"}, {"id": "qq:b", "name": "乙"}
    ledger.put(row(actor=b, participants=[a, b]))
    ledger.put(row(actor=a, kind="memory") | {"actor": None, "participants": []})
    trigger = ledger.report(rules(), days=0)
    assert next(p for p in trigger["people"] if p["id"] == b["id"])["input"] == 100
    split = ledger.report(rules(), days=0, attribution="participants")
    assert next(p for p in split["people"] if p["id"] == a["id"])["input"] == 50
    assert sum(p["cost"]["CNY"] for p in split["people"]) == pytest.approx(
        split["summary"]["cost"]["CNY"]
    )
    selected = ledger.report(rules(), days=0, attribution="participants", actor="qq:a")
    assert selected["summary"]["input"] == 50
    shared = ledger.report(rules(), days=0, actor="__shared__")
    assert shared["count"] == 1 and shared["summary"]["input"] == 100
    ledger.close()


def test_history_is_deduplicated_and_counted_as_turn_not_call(tmp_path):
    ledger = Ledger(tmp_path / "usage.db")
    historical = row(unit="turn", kind="history") | {"actor": None, "participants": []}
    ledger.put(historical, "native:1")
    ledger.put(historical, "native:1")
    summary = ledger.report([], days=0)["summary"]
    assert summary["turns"] == 1 and summary["calls"] == 0
    ledger.close()


class Event:
    def __init__(self, uid):
        self.uid, self.extra = uid, {}
        self.unified_msg_origin = "qq:GroupMessage:g1"
        self.cleanup_temporary_local_files = lambda: None

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.uid

    def get_platform_id(self):
        return "qq"

    def get_extra(self, name):
        return self.extra.get(name)

    def set_extra(self, name, value):
        self.extra[name] = value


def test_real_openai_adapter_copy_async_scopes_and_no_prompt_retention(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))
        provider = ProviderOpenAIOfficial(
            {
                "id": "deepseek",
                "type": "openai_chat_completion",
                "key": ["test-key"],
                "api_base": "https://example.invalid/v1",
                "model": "model",
            },
            {},
        )
        original = type(provider).text_chat
        collector.wrap_provider(type(provider), "text_chat")
        provider.client.chat.completions.create = AsyncMock(
            return_value=ChatCompletion(
                id="receipt",
                model="model",
                object="chat.completion",
                created=1,
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_cache_hit_tokens": 60,
                },
            )
        )
        try:
            collector.bind_event(Event("alice"))
            response = await asyncio.wait_for(
                copy.copy(provider).text_chat(prompt="PRIVATE-TEXT-DO-NOT-STORE"), 3
            )
            assert response.completion_text == "ok"
            first = ledger.report(rules(), days=0)
            assert first["summary"]["cache_rate"] == 0.6
            assert first["people"][0]["id"] == "qq:alice"
            assert "PRIVATE-TEXT" not in str(
                ledger.db.execute("SELECT data FROM receipts").fetchall()
            )
            assert "test-key" not in str(
                ledger.db.execute("SELECT data FROM receipts").fetchall()
            )
            await provider.client.close()
        finally:
            collector.stop()
        assert type(provider).text_chat is original

    asyncio.run(run())


def test_concurrent_event_contexts_and_stale_background_scope(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))

        class Provider:
            provider_config = {"id": "deepseek"}

            def get_model(self):
                return "model"

            async def text_chat(self):
                await asyncio.sleep(0.001)
                return LLMResponse(role="assistant", usage=TokenUsage(100, 50, 20))

        collector.wrap_provider(Provider, "text_chat")

        async def call(uid):
            event = Event(uid)
            collector.bind_event(event)
            await Provider().text_chat()
            event.cleanup_temporary_local_files()
            await Provider().text_chat()

        await asyncio.gather(call("a"), call("b"))
        people = {p["id"]: p for p in ledger.report([], days=0)["people"]}
        assert people["qq:a"]["calls"] == people["qq:b"]["calls"] == 1
        assert people[""]["calls"] == 2
        collector.stop()

    asyncio.run(run())


def test_api_prices_report_and_error_rollback(tmp_path):
    routes = {}

    def register(route, handler, methods, desc):
        routes[route.rsplit("/", 1)[1]] = handler

    context = SimpleNamespace(
        register_web_api=register,
        get_all_providers=lambda: [],
        get_config=lambda: {"timezone": "Asia/Shanghai"},
    )
    schema = {"prices_json": {"type": "text", "default": "[]"}}
    config = AstrBotConfig(str(tmp_path / "config.json"), schema=schema)
    with patch(
        "astrbot_plugin_usage_insight.main.StarTools.get_data_dir",
        return_value=tmp_path,
    ):
        plugin = UsageInsight(context, config)
    plugin.ledger.put(row())
    app = FastAPI()

    @app.api_route("/{name}", methods=["GET", "POST"])
    async def api(name: str, request: Request):
        with bind_request_context(
            PluginRequest(
                request, plugin_name="astrbot_plugin_usage_insight", username="test"
            )
        ):
            return await routes[name]()

    async def check_api():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/prices", json={"prices": rules()})
            assert response.status_code == 200
            response = await client.get("/report?days=0")
            body = response.json()
            assert body["summary"]["priced_count"] == 1
            assert isinstance(body["kinds"], list) and isinstance(
                body["kind_labels"], dict
            )
            assert (
                await client.post("/prices", json={"prices": [{"model": "x"}]})
            ).status_code == 400
            assert (await client.get("/report?days=-1")).status_code == 400
            before = config["prices_json"]
            with patch.object(
                type(config), "save_config", side_effect=OSError("not writable")
            ):
                failed = await client.post("/prices", json={"prices": []})
                assert failed.status_code == 500, failed.text
            assert config["prices_json"] == before

    asyncio.run(check_api())
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8-sig"))
    assert json.loads(saved["prices_json"]) == rules()
    plugin.collector.stop()


def test_regular_error_streaming_and_failed_ledger_do_not_change_business_result(
    tmp_path,
):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))
        problem = RuntimeError("PRIVATE-API-ERROR")

        class Provider:
            provider_config = {"id": "deepseek"}

            def get_model(self):
                return "model"

            async def text_chat(self, fail=False):
                if fail:
                    raise problem
                return LLMResponse(
                    role="assistant",
                    completion_text="original",
                    usage=TokenUsage(50, 50, 10),
                )

            async def text_chat_stream(self):
                yield LLMResponse(
                    role="assistant",
                    completion_text="o",
                    is_chunk=True,
                    usage=TokenUsage(50, 50, 5),
                )
                yield await self.text_chat()

        collector.wrap_provider(Provider, "text_chat")
        collector.wrap_provider(Provider, "text_chat_stream")
        try:
            with pytest.raises(RuntimeError) as caught:
                await Provider().text_chat(fail=True)
            assert caught.value is problem
            streamed = [r async for r in Provider().text_chat_stream()]
            assert streamed[-1].completion_text == "original"
            report = ledger.report([], days=0)
            assert (
                report["count"] == 2
            )  # nested stream implementation is not billed twice
            assert report["summary"]["total"] == 110
            assert "PRIVATE-API-ERROR" not in str(
                ledger.db.execute("SELECT data FROM receipts").fetchall()
            )
            with patch.object(ledger, "put", side_effect=OSError("disk unavailable")):
                result = await Provider().text_chat()
                assert result.completion_text == "original"
        finally:
            collector.stop()

    asyncio.run(run())


def test_scoped_memory_wait_for_has_no_trigger_user_and_restores_parent(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))

        class Provider:
            provider_config = {"id": "deepseek"}

            def get_model(self):
                return "model"

            async def text_chat(self):
                return LLMResponse(role="assistant", usage=TokenUsage(50, 50, 10))

        class Summarizer:
            async def summarize(self, scope):
                return await asyncio.wait_for(Provider().text_chat(), 1)

        collector.wrap_provider(Provider, "text_chat")
        collector.wrap_scoped_function(Summarizer, "summarize", "memory", scope_index=1)
        collector.bind_event(Event("alice"))
        try:
            await Summarizer().summarize({"origin": "qq:GroupMessage:g1"})
            await Provider().text_chat()
            report = ledger.report([], days=0)
            kinds = {r["kind"]: r for r in report["recent"]}
            assert kinds["memory"]["actor"] is None
            assert kinds["memory"]["origin"] == "qq:GroupMessage:g1"
            assert kinds["reply"]["actor"]["id"] == "qq:alice"
        finally:
            collector.stop()

    asyncio.run(run())


def test_direct_decision_response_is_reused_and_unrelated_http_ignored(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=raw_usage()))
        original = AsyncMock(return_value=response)
        collector.wrap_http(original)
        token = BOUND.set(
            {
                "kind": "decision",
                "origin": "group",
                "actor": {"id": "qq:a", "name": "甲"},
                "participants": [],
            }
        )
        payload = {
            "model": "decision-model-preview",
            "state": {"private": "PRIVATE-REQUEST"},
            "questions": {},
        }
        try:
            result = await aiohttp.ClientSession._request(
                object(), "POST", "https://example.invalid/systemone", json=payload
            )
            assert result is response
            assert await result.json() == raw_usage()
            await aiohttp.ClientSession._request(
                object(), "GET", "https://example.invalid/other"
            )
            report = ledger.report([], days=0)
            assert report["count"] == 1
            assert report["summary"]["cache_rate"] == 0.6
            assert "PRIVATE-REQUEST" not in str(
                ledger.db.execute("SELECT data FROM receipts").fetchall()
            )
        finally:
            BOUND.reset(token)
            collector.stop()

    asyncio.run(run())


def test_actual_native_history_import_cutoff_dedup_and_unknown_cache(tmp_path):
    async def run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + str(tmp_path / "native.db")
        )
        async with engine.begin() as connection:
            await connection.run_sync(ProviderStat.__table__.create)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSession(engine) as session:
            session.add_all(
                [
                    ProviderStat(
                        umo="group",
                        provider_id="deepseek",
                        provider_model="model",
                        created_at=now - timedelta(days=1),
                        token_input_other=100,
                        token_input_cached=50,
                        token_output=20,
                    ),
                    ProviderStat(
                        umo="group",
                        provider_id="deepseek",
                        provider_model="model",
                        created_at=now - timedelta(days=2),
                        token_input_other=100,
                        token_input_cached=0,
                        token_output=10,
                    ),
                    ProviderStat(
                        umo="group",
                        provider_id="deepseek",
                        provider_model="model",
                        created_at=now + timedelta(days=1),
                        token_input_other=9999,
                    ),
                ]
            )
            await session.commit()

        @asynccontextmanager
        async def get_db():
            async with AsyncSession(engine) as session:
                yield session

        context = SimpleNamespace(
            register_web_api=lambda *args: None,
            get_all_providers=lambda: [],
            get_config=lambda: {"timezone": "Asia/Shanghai"},
        )
        config = AstrBotConfig(
            str(tmp_path / "plugin.json"),
            schema={"prices_json": {"type": "text", "default": "[]"}},
        )
        with patch(
            "astrbot_plugin_usage_insight.main.StarTools.get_data_dir",
            return_value=tmp_path,
        ):
            plugin = UsageInsight(context, config)
        try:
            with patch("astrbot_plugin_usage_insight.main.db_helper.get_db", get_db):
                await plugin.import_history()
                await plugin.import_history()
            report = plugin.ledger.report([], days=0)
            assert plugin.import_status == "complete"
            assert report["count"] == 2
            assert report["summary"]["calls"] == 0
            assert report["summary"]["turns"] == 2
            assert report["summary"]["input"] == 250
            assert report["summary"]["cache_input"] == 150
            assert report["people"][0]["id"] == ""
        finally:
            plugin.collector.stop()
            await engine.dispose()

    asyncio.run(run())


def test_termination_waits_for_inflight_and_no_double_wrap(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        collector = Collector(ledger, lambda: True, logging.getLogger("test"))
        ready, proceed = asyncio.Event(), asyncio.Event()

        class Provider:
            provider_config = {"id": "deepseek"}

            def get_model(self):
                return "model"

            async def text_chat(self):
                ready.set()
                await proceed.wait()
                return LLMResponse(role="assistant", usage=TokenUsage(100, 20, 5))

        original = Provider.text_chat
        collector.wrap_provider(Provider, "text_chat")
        wrapper = Provider.text_chat
        collector.wrap_provider(Provider, "text_chat")
        assert Provider.text_chat is wrapper
        task = asyncio.create_task(Provider().text_chat())
        await ready.wait()
        collector.stop()
        assert Provider.text_chat is original
        proceed.set()
        await task
        reopened = Ledger(tmp_path / "usage.db")
        assert reopened.report([], days=0)["count"] == 1
        reopened.close()

    asyncio.run(run())


def test_refresh_handles_external_adapter_and_collection_switch(tmp_path):
    async def run():
        ledger = Ledger(tmp_path / "usage.db")
        state = {"enabled": True}
        collector = Collector(
            ledger, lambda: state["enabled"], logging.getLogger("test")
        )

        class ExternalProvider:
            provider_config = {"id": "external"}

            def get_model(self):
                return "model"

            async def text_chat(self):
                return LLMResponse(
                    role="assistant", completion_text="same", usage=TokenUsage(1, 1, 1)
                )

        provider = ExternalProvider()
        original = ExternalProvider.text_chat
        try:
            collector.refresh([provider])
            collector.refresh([provider])
            assert (await provider.text_chat()).completion_text == "same"
            state["enabled"] = False
            assert (await provider.text_chat()).completion_text == "same"
            assert ledger.report([], days=0)["count"] == 1
        finally:
            collector.stop()
        assert ExternalProvider.text_chat is original

    asyncio.run(run())
