import asyncio
import json
from datetime import datetime, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from astrbot.api import AstrBotConfig
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_usage_insight.ledger import prices_from_json
from astrbot_plugin_usage_insight.main import UsageInsight


@pytest.fixture
def setup(tmp_path):
    context = SimpleNamespace(
        get_all_providers=lambda: [],
        register_web_api=lambda *args: None,
        get_config=lambda: {"timezone": "Asia/Shanghai"},
    )
    config = AstrBotConfig(str(tmp_path / "config.json"), schema={})
    config["prices_json"] = json.dumps(
        [
            {"provider": "p", "model": "cheap", "input": 1, "cached": 1, "output": 1},
            {
                "provider": "p",
                "model": "expensive",
                "input": 20,
                "cached": 20,
                "output": 20,
            },
            {
                "provider": "p",
                "model": "usd",
                "input": 5,
                "cached": 5,
                "output": 5,
                "currency": "USD",
            },
            {"provider": "p", "model": "free", "input": 0, "cached": 0, "output": 0},
        ]
    )
    with patch(
        "astrbot_plugin_usage_insight.main.StarTools.get_data_dir",
        return_value=tmp_path,
    ):
        plugin = UsageInsight(context, config)
    plugin.import_status = "complete"
    event = SimpleNamespace(
        unified_msg_origin="qq:group:g1",
        get_sender_id=lambda: "u1",
        get_platform_id=lambda: "qq",
        get_sender_name=lambda: "小幸",
    )

    def add(uid="u1", name="小幸", model="cheap", tokens=1000000, **changes):
        actor = {"id": "qq:" + uid, "name": name} if uid else None
        row = {
            "ts": datetime.now(ZoneInfo("Asia/Shanghai")).timestamp(),
            "duration": 1,
            "provider": "p",
            "model": model,
            "kind": "reply",
            "origin": event.unified_msg_origin,
            "actor": actor,
            "participants": [actor] if actor else [],
            "unit": "call",
            "status": "completed",
            "usage": {"input": tokens, "output": 0, "cached": 0},
            "balance_ref": "private-key-tag-not-for-model",
            **changes,
        }
        plugin.ledger.put(row)

    add()
    add("u2", "测试乙", "expensive", 100000)
    yield plugin, event, add
    asyncio.run(plugin.terminate())


def query(setup, **kwargs):
    plugin, event, _ = setup
    return json.loads(asyncio.run(plugin.get_usage_stats(event, **kwargs)))


def test_totals_and_ranking_use_money_not_tokens_and_match_page(setup):
    plugin, _, _ = setup
    result = query(setup)
    assert result["summary"]["tokens"] == 1100000
    assert result["summary"]["cost"] == {"CNY": 3}
    ranking = result["rankings"]["CNY"]["entries"]
    assert [p["id"] for p in ranking] == ["qq:u2", "qq:u1"]
    assert [p["amount"] for p in ranking] == [2, 1]
    page = plugin.ledger.report(
        prices_from_json(plugin.config["prices_json"]), catalog=plugin.pricing
    )
    assert result["summary"]["cost"] == page["summary"]["cost"]
    text = json.dumps(result)
    for field in (
        "cache_rate",
        "input_ratio",
        "output_ratio",
        "cost_parts",
        "provider",
        "price_snapshot",
        "balance_ref",
        "private-key-tag",
    ):
        assert field not in text


@pytest.mark.parametrize("person", ["小幸", "u1", "qq:u1", "me", "我"])
def test_person_lookup_returns_only_target_and_no_rankings(setup, person):
    result = query(setup, person=person)
    assert result["ok"] and result["person"]["id"] == "qq:u1"
    assert result["summary"]["cost"] == {"CNY": 1}
    assert result["summary"]["tokens"] == 1000000
    assert "rankings" not in result


def test_same_name_is_not_merged_and_exact_id_resolves_it(setup):
    _, _, add = setup
    add("u3", "小幸", tokens=2000000)
    result = query(setup, person="小幸")
    assert result["error"] == "ambiguous_person"
    assert {p["id"] for p in result["candidates"]} == {"qq:u1", "qq:u3"}
    assert query(setup, person="qq:u3")["summary"]["cost"] == {"CNY": 2}


def test_current_chat_scope_excludes_other_chats_but_all_keeps_them(setup):
    _, _, add = setup
    add("u3", "其他群", tokens=5000000, origin="qq:group:g2")
    assert query(setup, scope="current_chat")["summary"]["cost"] == {"CNY": 3}
    assert query(setup, scope="all")["summary"]["cost"] == {"CNY": 8}


def test_currencies_rank_separately_and_known_zero_remains_zero(setup):
    _, _, add = setup
    add("u3", "美元", "usd", 100000)
    add("u4", "免费", "free", 100000)
    result = query(setup)
    assert result["summary"]["cost"] == {"CNY": 3, "USD": 0.5}
    assert result["rankings"]["USD"]["entries"][0]["amount"] == 0.5
    assert result["rankings"]["CNY"]["entries"][-1]["amount"] == 0
    assert result["rankings"]["CNY"]["entries"][-1]["cost_state"] == "estimated"


def test_unknown_cost_and_missing_usage_do_not_rank_as_free(setup):
    _, _, add = setup
    add("u3", "未知价格", "no-price", 500000)
    add("u4", "未知用量", usage=None)
    add("u1", "小幸", "no-price", 10)
    result = query(setup)
    assert result["summary"]["cost_state"] == "partial"
    assert result["summary"]["tokens_state"] == "partial"
    assert result["people_without_priced_cost_total"] == 2
    assert {p["id"] for p in result["people_without_priced_cost"]} == {"qq:u3", "qq:u4"}
    assert len(result["rankings"]["CNY"]["entries"]) == 2
    assert result["rankings"]["CNY"]["entries"][1]["cost_state"] == "partial"
    unknown = query(setup, person="qq:u4")["summary"]
    assert unknown["tokens"] is None and unknown["cost"] == {}
    assert unknown["tokens_state"] == unknown["cost_state"] == "unknown"


def test_background_and_history_are_outside_person_ranking(setup):
    _, _, add = setup
    add("", kind="memory")
    add("", kind="history", unit="turn")
    add("", kind="other")
    result = query(setup)
    assert result["summary"]["cost"] == {"CNY": 6}
    assert len(result["rankings"]["CNY"]["entries"]) == 2
    assert {p["name"] for p in result["unattributed"]} == {
        "后台公共开销",
        "宿主历史（未记录用户）",
        "未识别触发者",
    }


def test_days_use_local_calendar_and_all_includes_older_records(setup):
    plugin, _, add = setup
    plugin.ledger.db.execute("DELETE FROM receipts")
    plugin.ledger.db.commit()
    zone = ZoneInfo("Asia/Shanghai")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 24, 0, 30, tzinfo=zone).astimezone(tz)

    for days, hour, amount in ((0, 0, 1000000), (1, 23, 2000000), (8, 12, 3000000)):
        ts = (
            datetime(2026, 10, 24, hour, tzinfo=zone) - timedelta(days=days)
        ).timestamp()
        add(ts=ts, tokens=amount)
    with patch("astrbot_plugin_usage_insight.ledger.datetime", FixedDateTime):
        assert query(setup, days=1)["summary"]["cost"] == {"CNY": 1}
        assert query(setup, days=7)["summary"]["cost"] == {"CNY": 3}
        assert query(setup, days=0)["summary"]["cost"] == {"CNY": 6}


def test_empty_self_and_missing_person_are_explicit(setup):
    plugin, _, _ = setup
    assert query(setup, person="不存在的人")["error"] == "person_not_found"
    plugin.ledger.db.execute("DELETE FROM receipts")
    plugin.ledger.db.commit()
    result = query(setup, person="me")
    assert result["summary"]["tokens"] is None
    assert result["summary"]["cost_state"] == "no_records"
    assert query(setup)["rankings"] == {}


def test_rank_ties_and_limits_keep_people_distinct(setup):
    _, _, add = setup
    add("u3", "测试丙", tokens=2000000)
    entries = query(setup, limit=2)["rankings"]["CNY"]["entries"]
    assert len(entries) == 2 and [p["rank"] for p in entries] == [1, 1]
    assert {p["id"] for p in entries} == {"qq:u2", "qq:u3"}


@pytest.mark.parametrize(
    "args",
    [
        {"days": -1},
        {"days": True},
        {"days": "7"},
        {"days": 7.5},
        {"limit": 51},
        {"scope": "bad"},
        {"person": None},
    ],
)
def test_invalid_queries_return_safe_errors(setup, args):
    result = query(setup, **args)
    assert not result["ok"] and result["error"] == "invalid_query"


def test_native_tool_executor_passes_registered_arguments_without_network_or_messages(
    setup,
):
    plugin, event, _ = setup
    tool = llm_tools.get_func("get_usage_stats")
    assert set(tool.parameters["properties"]) == {"days", "person", "scope", "limit"}
    assert tool.parameters["properties"]["days"]["type"] == "number"
    bound = FunctionTool(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        handler=partial(tool.handler, plugin),
    )
    event.send = AsyncMock(side_effect=AssertionError("must not send"))
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=10
    )

    async def run():
        with patch.object(
            plugin.balances,
            "query",
            new_callable=AsyncMock,
            side_effect=AssertionError("must not query account"),
        ) as balance:
            results = [
                r
                async for r in FunctionToolExecutor.execute(
                    bound,
                    context,
                    days=7.0,
                    person="小幸",
                    scope="current_chat",
                    limit=10.0,
                )
            ]
            balance.assert_not_awaited()
        result = json.loads(results[0].content[0].text)
        assert result["ok"] and result["summary"]["cost"] == {"CNY": 1}
        event.send.assert_not_awaited()

    asyncio.run(run())
