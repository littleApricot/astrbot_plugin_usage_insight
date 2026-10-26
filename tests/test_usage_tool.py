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


def test_compact_totals_and_full_ranking_match_page(setup):
    plugin, _, _ = setup
    result = query(setup)
    assert result == {
        "total_tokens": 1100000,
        "total_cost": 3,
        "people": [
            {"name": "测试乙", "tokens": 100000, "cost": 2},
            {"name": "小幸", "tokens": 1000000, "cost": 1},
        ],
    }
    page = plugin.ledger.report(
        prices_from_json(plugin.config["prices_json"]), catalog=plugin.pricing
    )
    assert result["total_cost"] == page["summary"]["cost"]["CNY"]


def test_all_members_are_returned_without_truncation(setup):
    _, _, add = setup
    for number in range(3, 24):
        add("u" + str(number), "成员" + str(number), tokens=number * 1000000)
    people = query(setup)["people"]
    assert len(people) == 23
    assert [p["cost"] for p in people] == sorted(
        (p["cost"] for p in people), reverse=True
    )
    assert all(set(p) == {"name", "tokens", "cost"} for p in people)


def test_same_names_remain_separate_rows(setup):
    _, _, add = setup
    add("u3", "小幸", tokens=2000000)
    people = [p for p in query(setup)["people"] if p["name"] == "小幸"]
    assert [p["cost"] for p in people] == [2, 1]


def test_cny_amounts_do_not_convert_or_mislabel_other_currencies(setup):
    _, _, add = setup
    add("u3", "非人民币记录", "usd", 100000)
    add("u4", "免费", "free", 100000)
    result = query(setup)
    assert result["total_cost"] == 3
    assert result["incomplete"] == ["cost"]
    assert (
        next(p for p in result["people"] if p["name"] == "非人民币记录")["cost"] is None
    )
    assert next(p for p in result["people"] if p["name"] == "免费")["cost"] == 0
    assert "USD" not in json.dumps(result)


def test_partial_totals_keep_known_values_and_mark_missing_fields(setup):
    _, _, add = setup
    add("u3", "未知价格", "no-price", 500000)
    add("u4", "未知用量", usage=None)
    add("u1", "小幸", "no-price", 10)
    result = query(setup)
    assert result["total_cost"] == 3 and result["total_tokens"] == 1600010
    assert result["incomplete"] == ["tokens", "cost"]
    assert "failed_without_usage" not in result
    people = {p["name"]: p for p in result["people"]}
    assert people["未知价格"]["tokens"] == 500000
    assert people["未知价格"]["cost"] is None
    assert people["未知价格"]["incomplete"] == ["cost"]
    assert people["未知用量"]["tokens"] is None
    assert people["未知用量"]["cost"] is None
    assert people["未知用量"]["incomplete"] == ["tokens", "cost"]
    assert people["小幸"]["cost"] == 1
    assert people["小幸"]["incomplete"] == ["cost"]
    assert result["people"][0]["name"] == "测试乙"


def test_codex_and_bailian_are_excluded_only_from_tool_report(setup):
    plugin, _, add = setup
    for provider, kind in (
        ("codex-cli", "recognition"), ("bailian-decision", "decision")
    ):
        for status in ("completed", "error"):
            add(
                "excluded", "排除来源", provider=provider, kind=kind,
                status=status, usage=None,
            )
    # The same purposes through other providers remain billable.
    add(kind="recognition", tokens=200000)
    add(kind="decision", tokens=300000)
    result = query(setup)
    assert result["total_tokens"] == 1600000
    assert result["total_cost"] == pytest.approx(3.5)
    assert "incomplete" not in result and "failed_without_usage" not in result
    assert len(result["people"]) == 2
    assert next(p for p in result["people"] if p["name"] == "小幸")["cost"] == 1.5
    page = plugin.ledger.report(prices_from_json(plugin.config["prices_json"]))
    assert page["count"] == 8
    assert page["summary"]["failed_without_usage"] == 2


def test_failures_keep_known_totals_and_mark_affected_members(setup):
    _, _, add = setup
    for status, error in (
        ("error", "BadRequestError"),
        ("error", "APITimeoutError"),
        ("cancelled", "CancelledError"),
    ):
        add(status=status, error=error, usage=None)
    # A failed request with an actual usage receipt still contributes its known cost.
    add(status="error", tokens=1500000)
    result = query(setup)
    assert result["total_tokens"] == 2600000
    assert result["total_cost"] == 4.5
    assert result["incomplete"] == ["tokens", "cost"]
    assert result["failed_without_usage"] == 3
    assert result["people"] == [
        {
            "name": "小幸", "tokens": 2500000, "cost": 2.5,
            "incomplete": ["tokens", "cost"], "failed_without_usage": 3,
        },
        {"name": "测试乙", "tokens": 100000, "cost": 2},
    ]


def test_all_unknown_usage_is_not_reported_as_free(setup):
    plugin, _, add = setup
    plugin.ledger.db.execute("DELETE FROM receipts")
    plugin.ledger.db.commit()
    add(status="cancelled", usage=None)
    result = query(setup)
    assert result["total_tokens"] is None and result["total_cost"] is None
    assert result["incomplete"] == ["tokens", "cost"]
    assert result["failed_without_usage"] == 1


def test_failed_compression_marks_total_and_compression_only(setup):
    _, _, add = setup
    add(kind="compression", tokens=200000)
    add(kind="compression", status="error", usage=None)
    result = query(setup)
    assert result["total_cost"] == pytest.approx(3.2)
    assert result["failed_without_usage"] == 1
    assert result["compression"] == {
        "tokens": 200000, "cost": 0.2,
        "incomplete": ["tokens", "cost"], "failed_without_usage": 1,
    }
    assert all("incomplete" not in person for person in result["people"])


def test_compression_is_in_total_but_not_charged_to_a_person(setup):
    _, _, add = setup
    add("u1", kind="compression", tokens=200000)
    add("", kind="memory", tokens=300000)
    result = query(setup)
    assert result["total_tokens"] == 1600000
    assert result["total_cost"] == pytest.approx(3.5)
    assert result["compression"] == {"tokens": 200000, "cost": 0.2}
    assert len(result["people"]) == 2
    assert next(p for p in result["people"] if p["name"] == "小幸")["tokens"] == 1000000
    assert "other_app_usage" not in result


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
        assert query(setup, days=1)["total_cost"] == 1
        assert query(setup, days=7)["total_cost"] == 3
        assert query(setup, days=0)["total_cost"] == 6


def test_no_collected_records_has_empty_ranking(setup):
    plugin, _, _ = setup
    plugin.ledger.db.execute("DELETE FROM receipts")
    plugin.ledger.db.commit()
    assert query(setup) == {"total_tokens": 0, "total_cost": 0, "people": []}


@pytest.mark.parametrize("days", [-1, True, "7", 7.5, float("nan"), float("inf")])
def test_invalid_days_return_safe_errors(setup, days):
    result = query(setup, days=days)
    assert not result["ok"] and result["error"] == "invalid_query"


def test_native_tool_executor_exposes_only_days(setup):
    plugin, event, _ = setup
    tool = llm_tools.get_func("get_usage_stats")
    assert set(tool.parameters["properties"]) == {"days"}
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
                r async for r in FunctionToolExecutor.execute(bound, context, days=7.0)
            ]
            balance.assert_not_awaited()
        result = json.loads(results[0].content[0].text)
        assert result["total_cost"] == 3
        assert len(result["people"]) == 2
        event.send.assert_not_awaited()

    asyncio.run(run())


def test_lcm_wait_for_compression_is_captured_without_inherited_user(setup):
    from astrbot_plugin_usage_insight.capture import BOUND

    plugin, _, _ = setup

    class Provider:
        provider_config = {"id": "p"}

        def get_model(self):
            return "cheap"

        async def text_chat(self, **kwargs):
            return SimpleNamespace(
                role="assistant",
                raw_completion={
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_cache_hit_tokens": 50,
                    }
                },
            )

    provider = Provider()
    plugin.collector.refresh([provider])
    namespace = {"__name__": "astrbot_plugin_lcm.fake_worker", "asyncio": asyncio}
    exec(
        "async def work(provider):\n    scope = {'origin': 'qq:group:g1'}\n    return await asyncio.wait_for(provider.text_chat(prompt='summary'), timeout=3)\n",
        namespace,
    )

    async def run():
        parent = {
            "kind": "reply",
            "origin": "qq:group:g1",
            "actor": {"id": "qq:u1", "name": "小幸"},
            "participants": [],
        }
        token = BOUND.set(parent)
        try:
            await namespace["work"](provider)
            assert BOUND.get() is parent
        finally:
            BOUND.reset(token)

    asyncio.run(run())
    rows = plugin.ledger.report(prices_from_json(plugin.config["prices_json"]), days=0)[
        "recent"
    ]
    compression = next(row for row in rows if row["kind"] == "compression")
    assert compression["actor"] is None and compression["participants"] == []
    assert compression["origin"] == "qq:group:g1"
    assert compression["usage"]["input"] == 100
