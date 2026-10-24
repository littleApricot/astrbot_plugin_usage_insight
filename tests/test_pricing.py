import asyncio
import copy
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_usage_insight.capture import BOUND, Collector, metadata
from astrbot_plugin_usage_insight.ledger import Ledger, estimate, prices_from_json
from astrbot_plugin_usage_insight.pricing import (
    PricingCatalog,
    api_host,
    parse_official,
    period_at,
)


def official_html():
    return """<table><tr><th>模型</th><th>deepseek-flash(1)</th><th>deepseek-v4-pro</th></tr>
    <tr><td>百万tokens输入（缓存命中）</td><td>空闲时段</td><td>0.02元</td><td>0.15元</td></tr>
    <tr><td>高峰时段</td><td>0.04元</td><td>0.30元</td></tr>
    <tr><td>百万tokens输入（缓存未命中）</td><td>空闲时段</td><td>1元</td><td>4.5元</td></tr>
    <tr><td>高峰时段</td><td>2元</td><td>9.0元</td></tr>
    <tr><td>百万tokens输出</td><td>空闲时段</td><td>4元</td><td>13.5元</td></tr>
    <tr><td>高峰时段</td><td>8元</td><td>27.0元</td></tr></table>
    <p>北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；
    其余时段，包括周末及中国法定节假日全天均为空闲时段。</p>"""


def timestamp(local):
    return (
        datetime.fromisoformat(local)
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .timestamp()
    )


def sample(**changes):
    return {
        "provider": "official",
        "model": "deepseek-flash",
        "api_host": "api.deepseek.com",
        "ts": timestamp("2026-10-23T09:00:00"),
        "usage": {"input": 1000000, "cached": 600000, "output": 1000000},
        "kind": "reply",
        "origin": "synthetic-group",
        "actor": {"id": "qq:a", "name": "测试甲"},
        "participants": [{"id": "qq:a", "name": "测试甲"}],
        "duration": 1,
        "unit": "call",
        "status": "completed",
        **changes,
    }


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("2026-10-23T08:59:59", "off_peak"),
        ("2026-10-23T09:00:00", "peak"),
        ("2026-10-23T11:59:59", "peak"),
        ("2026-10-23T12:00:00", "off_peak"),
        ("2026-10-23T13:59:59", "off_peak"),
        ("2026-10-23T14:00:00", "peak"),
        ("2026-10-23T17:59:59", "peak"),
        ("2026-10-23T18:00:00", "off_peak"),
        ("2026-10-24T10:00:00", "off_peak"),
        ("2026-10-01T10:00:00", "off_peak"),
        ("2026-10-07T10:00:00", "off_peak"),
        ("2026-10-08T10:00:00", "peak"),
        ("2026-10-10T10:00:00", "off_peak"),
        ("2027-01-04T10:00:00", None),
    ],
)
def test_request_time_peak_boundaries_holidays_and_makeup_weekend(date, expected):
    assert period_at(timestamp(date)) == expected


def test_official_parser_rejects_changed_price_table_or_schedule():
    parsed = parse_official(official_html())
    assert parsed["models"]["deepseek-flash"]["peak"]["input"] == "2"
    assert parsed["models"]["deepseek-v4-pro"]["off_peak"]["output"] == "13.5"
    for old, new in (
        ("9:00", "8:00"),
        ("deepseek-flash(1)", "new-model"),
        ("2元", "2美元"),
    ):
        with pytest.raises(ValueError):
            parse_official(official_html().replace(old, new))


def test_auto_prices_only_official_host_and_known_models_with_manual_override(tmp_path):
    catalog = PricingCatalog(tmp_path)
    catalog.bind_providers(
        [
            SimpleNamespace(
                provider_config={
                    "id": "official",
                    "api_base": "https://api.deepseek.com/v1",
                }
            )
        ]
    )
    peak = estimate(sample(), [], catalog)
    assert peak["total"] == pytest.approx(8.824)
    low = estimate(sample(ts=timestamp("2026-10-24T10:00:00")), [], catalog)
    assert low["total"] == pytest.approx(peak["total"] / 2)
    assert estimate(sample(api_host="proxy.example.invalid"), [], catalog) is None
    assert estimate(sample(model="unlisted-model"), [], catalog) is None
    assert (
        estimate(sample(model="deepseek-v4-flash"), [], catalog)["total"]
        == peak["total"]
    )
    assert catalog.resolve({k: v for k, v in sample().items() if k != "api_host"})
    overrides = prices_from_json(
        [
            {
                "provider": "official",
                "model": "deepseek-flash",
                "input": 1,
                "cached": 1,
                "output": 1,
            }
        ]
    )
    assert estimate(sample(), overrides, catalog)["total"] == 2
    assert (
        api_host({"api_base": "https://name:PRIVATE@api.deepseek.com/v1?key=PRIVATE"})
        == "api.deepseek.com"
    )
    assert "PRIVATE" not in json.dumps(catalog.status())


def test_saved_auto_snapshot_stays_stable_and_legacy_records_are_labelled(tmp_path):
    ledger, catalog = Ledger(tmp_path / "test.db"), PricingCatalog(tmp_path)
    collector = Collector(
        ledger, lambda: True, logging.getLogger("test"), catalog.resolve
    )
    try:
        collector.write(sample())
        before = ledger.report([], days=0, catalog=catalog)
        assert before["recent"][0]["cost"]["basis"] == "official_snapshot"
        catalog.data["models"]["deepseek-flash"]["peak"]["output"] = "80"
        after = ledger.report([], days=0, catalog=catalog)
        assert before["summary"]["cost"] == after["summary"]["cost"]
        ledger.put(sample(kind="history", unit="turn", actor=None, participants=[]))
        legacy = ledger.report([], days=0, actor="__history__", catalog=catalog)
        assert legacy["count"] == 1
        assert legacy["recent"][0]["cost"]["basis"] == "retrospective"
    finally:
        collector.stop()


def test_unknown_costs_and_attribution_are_explained_and_separate(tmp_path):
    ledger, catalog = Ledger(tmp_path / "test.db"), PricingCatalog(tmp_path)
    try:
        ledger.put(sample(kind="history", unit="turn", actor=None, participants=[]))
        ledger.put(sample(kind="memory", actor=None, participants=[]))
        ledger.put(sample(kind="other", actor=None, participants=[], usage=None))
        ledger.put(sample(usage={"input": 100, "cached": None, "output": 20}))
        report = ledger.report([], days=0, catalog=catalog)
        assert set(report["attribution_counts"]) == {
            "宿主历史（未记录用户）",
            "后台公共开销",
            "未识别触发者",
            "已归属用户",
        }
        assert report["unpriced_reasons"] == {"缺少缓存明细": 1, "接口未返回用量": 1}
        assert ledger.report([], days=0, actor="__history__")["count"] == 1
        assert ledger.report([], days=0, actor="__background__")["count"] == 1
        assert ledger.report([], days=0, actor="__unassigned__")["count"] == 1
        assert ledger.report([], days=0, actor="__shared__")["count"] == 3
        ledger.put(sample(ts=timestamp("2027-01-04T10:00:00")))
        assert (
            ledger.report([], days=0, catalog=catalog)["unpriced_reasons"][
                "缺少该年节假日历"
            ]
            == 1
        )
    finally:
        ledger.close()


def test_partial_caller_decision_metadata_keeps_explicit_event_actor():
    bound = {
        "kind": "reply",
        "actor": {"id": "qq:a", "name": "甲"},
        "participants": [],
        "origin": "group",
        "active": True,
    }
    token = BOUND.set(bound)
    try:
        found = {"kind": "decision", "actor": None, "participants": [], "origin": ""}
        with patch(
            "astrbot_plugin_usage_insight.capture.caller_metadata", return_value=found
        ):
            assert metadata()["actor"]["id"] == "qq:a"
            bound["active"] = False
            assert metadata()["actor"] is None
    finally:
        BOUND.reset(token)


def test_price_refresh_caches_valid_data_and_retains_last_good_on_failure(tmp_path):
    async def run():
        catalog = PricingCatalog(tmp_path)

        class Content:
            async def iter_chunked(self, size):
                body = official_html().encode()
                yield body[:90]
                yield body[90:]

        class Response:
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def raise_for_status(self):
                pass

        class Session(Response):
            def __init__(self, **kwargs):
                pass

            def get(self, *args, **kwargs):
                return Response()

        with patch(
            "astrbot_plugin_usage_insight.pricing.aiohttp.ClientSession", Session
        ):
            assert await catalog.refresh()
        assert catalog.state == "synced"
        saved = copy.deepcopy(catalog.data)
        with patch(
            "astrbot_plugin_usage_insight.pricing.aiohttp.ClientSession",
            side_effect=TimeoutError(),
        ):
            assert not await catalog.refresh()
        assert catalog.data == saved
        loaded = PricingCatalog(tmp_path)
        assert loaded.data == saved and loaded.state == "cached"

    asyncio.run(run())
