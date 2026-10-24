import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from astrbot.api import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_usage_insight.balance_api import query_deepseek_balance
from astrbot_plugin_usage_insight.balances import BalanceMonitor, supported
from astrbot_plugin_usage_insight.capture import Collector
from astrbot_plugin_usage_insight.ledger import Ledger, prices_from_json
from astrbot_plugin_usage_insight.main import UsageInsight
from astrbot_plugin_usage_insight.pricing import PricingCatalog


def provider(
    identifier="official", key="test-balance-key", base="https://api.deepseek.com/v1"
):
    return SimpleNamespace(
        provider_config={"id": identifier, "api_base": base},
        get_current_key=lambda: key,
    )


def balance(total="20.00", granted="0.00", topped=None, **extra):
    return {
        "ok": True,
        "platform": "DeepSeek",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": total,
                "granted_balance": granted,
                "topped_up_balance": topped or total,
            }
        ],
        **extra,
    }


def monitor_at(tmp_path, providers=None):
    providers = providers or [provider()]
    context = SimpleNamespace(get_all_providers=lambda: providers)
    ledger = Ledger(tmp_path / "usage.db")
    return BalanceMonitor(context, ledger, {}, PricingCatalog(tmp_path))


def usage_row(ref, **changes):
    return {
        "ts": 120,
        "duration": 1,
        "provider": "official",
        "model": "test-model",
        "kind": "reply",
        "origin": "synthetic-group",
        "actor": {"id": "qq:test", "name": "测试"},
        "participants": [],
        "unit": "call",
        "status": "completed",
        "usage": {"input": 1000000, "cached": 0, "output": 0},
        "balance_ref": ref,
        **changes,
    }


def one_yuan_rules():
    return prices_from_json(
        [
            {
                "provider": "official",
                "model": "test-model",
                "input": 1,
                "cached": 0,
                "output": 0,
            }
        ]
    )


@pytest.mark.parametrize(
    "status,error",
    [(401, "authentication_failed"), (429, "rate_limited"), (500, "http_error")],
)
def test_http_query_preserves_safe_errors_without_raw_body(status, error):
    async def run():
        original = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, text="PRIVATE-KEY-OR-BODY")
        )
        with patch(
            "astrbot_plugin_usage_insight.balance_api.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(**kwargs, transport=transport),
        ):
            result = await query_deepseek_balance("test-key")
        assert result["error"] == error
        assert "PRIVATE" not in json.dumps(result)

    asyncio.run(run())


def test_actual_httpx_request_uses_official_endpoint_and_whitelists_fields():
    async def run():
        original = httpx.AsyncClient

        def handle(request):
            assert str(request.url) == "https://api.deepseek.com/user/balance"
            assert request.headers["Authorization"] == "Bearer test-key"
            data = balance()
            data.pop("ok")
            data["PRIVATE"] = "PRIVATE-RESPONSE"
            return httpx.Response(200, json=data)

        with patch(
            "astrbot_plugin_usage_insight.balance_api.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(
                **kwargs, transport=httpx.MockTransport(handle)
            ),
        ):
            result = await query_deepseek_balance("test-key")
        assert result["ok"] and result["balance_infos"][0]["total_balance"] == "20.00"
        assert "PRIVATE" not in json.dumps(result)

    asyncio.run(run())


@pytest.mark.parametrize(
    "base",
    [
        "http://api.deepseek.com",
        "https://api.deepseek.com.evil.invalid",
        "https://name:secret@api.deepseek.com",
        "https://api.deepseek.com:444",
    ],
)
def test_platform_validation_does_not_send_keys_to_other_destinations(base):
    assert not supported(provider(base=base))


def test_shared_query_cache_groups_duplicate_key_and_failed_query_keeps_last_success(
    tmp_path,
):
    async def run():
        a, b = provider(), provider("official-vision")
        monitor = monitor_at(tmp_path, [a, b])
        try:
            with patch(
                "astrbot_plugin_usage_insight.balances.query_deepseek_balance",
                new_callable=AsyncMock,
                return_value=balance(),
            ) as query:
                await monitor.refresh()
                tool = await monitor.query(b)
                assert tool["provider_id"] == "official-vision" and tool["cached"]
                query.assert_awaited_once()
            ref = monitor.credential_ref(a)
            monitor.cache.clear()
            with patch(
                "astrbot_plugin_usage_insight.balances.query_deepseek_balance",
                new_callable=AsyncMock,
                return_value={"ok": False, "error": "timeout"},
            ):
                assert not (await monitor.query(a))["ok"]
            view = await monitor.view([])
            assert len(view["groups"]) == 1
            assert (
                view["groups"][0]["latest"]["balance_infos"][0]["total_balance"]
                == "20.00"
            )
            assert view["groups"][0]["last_error"]["error"] == "timeout"
            assert (
                monitor.ledger.db.execute(
                    "SELECT count(*) FROM balance_snapshots WHERE credential_ref=?",
                    (ref,),
                ).fetchone()[0]
                == 1
            )
            stored = str(
                monitor.ledger.db.execute(
                    "SELECT data FROM balance_snapshots"
                ).fetchall()
            )
            assert "test-balance-key" not in stored
        finally:
            monitor.ledger.close()

    asyncio.run(run())


def test_key_usage_excludes_other_agents_keys_and_untagged_history(tmp_path):
    monitor = monitor_at(tmp_path)
    try:
        ref = monitor.credential_ref(provider())
        other = monitor.credential_ref(provider(key="other-agent-test-key"))
        monitor.ledger.put(usage_row(ref))
        monitor.ledger.put(
            usage_row(other, usage={"input": 9000000, "cached": 0, "output": 0})
        )
        monitor.ledger.put(usage_row("", kind="history", unit="turn"))
        monitor.ledger.put(
            usage_row(
                ref,
                provider="old-provider",
                usage={"input": 500000, "cached": 0, "output": 0},
            )
        )
        rules = one_yuan_rules() + prices_from_json(
            [
                {
                    "provider": "old-provider",
                    "model": "test-model",
                    "input": 1,
                    "cached": 0,
                    "output": 0,
                }
            ]
        )
        report = monitor.report({ref: ["official"]}, rules, days=0)
        local = report["groups"][0]["local_usage"]
        assert local["calls"] == 2 and local["input"] == 1500000
        assert local["cost"]["CNY"] == 1.5
        assert report["groups"][0]["untagged_records"] == 1
        assert "balance_ref" not in json.dumps(report)
        assert report["usage_scope"] == "current_key_this_astrbot"
    finally:
        monitor.ledger.close()


def test_native_registered_tool_uses_selected_provider_and_shared_service(tmp_path):
    async def run():
        a = provider()
        context = SimpleNamespace(
            get_all_providers=lambda: [a],
            register_web_api=lambda *args: None,
            get_provider_by_id=lambda name: a,
            get_using_provider_async=AsyncMock(return_value=a),
            get_config=lambda: {"timezone": "Asia/Shanghai"},
        )
        config = AstrBotConfig(str(tmp_path / "config.json"), schema={})
        with patch(
            "astrbot_plugin_usage_insight.main.StarTools.get_data_dir",
            return_value=tmp_path,
        ):
            plugin = UsageInsight(context, config)
        event = SimpleNamespace(
            get_extra=lambda name: "official", unified_msg_origin="synthetic-group"
        )
        try:
            tool = llm_tools.get_func("get_self_balance")
            assert (
                len([t for t in llm_tools.func_list if t.name == "get_self_balance"])
                == 1
            )
            assert tool.parameters["properties"] == {}
            assert tool.handler.__module__ == "astrbot_plugin_usage_insight.main"
            with patch(
                "astrbot_plugin_usage_insight.balances.query_deepseek_balance",
                new_callable=AsyncMock,
                return_value=balance(),
            ) as query:
                result = json.loads(await partial(tool.handler, plugin)(event))
                page = await plugin.balances.view([])
                assert (
                    result["ok"]
                    and page["groups"][0]["latest"]["checked_at"]
                    == result["checked_at"]
                )
                assert (
                    result["local_usage_scope"] == "current_key_this_astrbot"
                    and result["local_usage_days"] == 7
                )
                context.get_using_provider_async.assert_not_awaited()
                query.assert_awaited_once()
        finally:
            await plugin.terminate()

    asyncio.run(run())


def test_collector_tags_stable_credential_but_does_not_assign_changed_key(tmp_path):
    async def run():
        monitor = monitor_at(tmp_path)

        class Provider:
            provider_config = {"id": "official", "api_base": "https://api.deepseek.com"}
            key = "test-first-key"

            def get_model(self):
                return "test-model"

            def get_current_key(self):
                return self.key

            async def text_chat(self, change=False):
                if change:
                    self.key = "test-second-key"
                return LLMResponse(
                    role="assistant",
                    raw_completion={
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}
                    },
                )

        p = Provider()
        collector = Collector(monitor.ledger, lambda: True, logging.getLogger("test"))
        collector.balance_scope = monitor.credential_ref
        collector.wrap_provider(Provider, "text_chat")
        try:
            first = monitor.credential_ref(p)
            await p.text_chat()
            await p.text_chat(change=True)
            records = [
                json.loads(r[0])
                for r in monitor.ledger.db.execute("SELECT data FROM receipts")
            ]
            assert (
                records[0]["balance_ref"] == first and records[1]["balance_ref"] == ""
            )
            assert "test-first-key" not in json.dumps(records)
        finally:
            collector.stop()

    asyncio.run(run())
