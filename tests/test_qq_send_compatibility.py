import asyncio
import json
import logging
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from astrbot_plugin_usage_insight import capture
from astrbot_plugin_usage_insight.accounting import caller_metadata
from astrbot_plugin_usage_insight.capture import BOUND, Collector
from botpy.api import BotAPI


class Event:
    unified_msg_origin = "qq:GroupMessage:test-group"

    def get_sender_id(self):
        return "test-user"

    def get_sender_name(self):
        return "测试用户"

    def get_platform_id(self):
        return "qq"

    def get_extra(self, name):
        return None


def collector():
    ledger = SimpleNamespace(put=lambda row: None, close=lambda: None)
    return Collector(ledger, lambda: True, logging.getLogger("qq-send-test"))


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("post_group_message", {"group_openid": "test-group"}),
        ("post_c2c_message", {"openid": "test-user"}),
    ],
)
def test_caller_metadata_does_not_change_real_botpy_payload(method, address):
    class FakeHTTP:
        async def request(self, route, **kwargs):
            payload = kwargs["json"]
            expected = dict(payload)
            capture.metadata()
            # Serialize first to preserve the exact regression failure:
            # frame.f_locals used to put BotAPI.self back into locals().
            aiohttp.payload.JsonPayload(payload)
            assert payload == expected
            assert not {"self", "payload", "route"}.intersection(payload)
            return {"id": "test-message"}

    async def run():
        api = BotAPI(FakeHTTP())
        result = await getattr(api, method)(content="测试消息", **address)
        assert result == {"id": "test-message"}

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "address"),
    [
        ("post_group_message", {"group_openid": "test-group"}),
        ("post_c2c_message", {"openid": "test-user"}),
    ],
)
def test_qq_send_through_collector_and_aiohttp_never_scans_callers(method, address):
    class BeforeNetwork(Exception):
        pass

    async def run():
        observer = collector()
        original = aiohttp.ClientSession._request
        observer.wrap_http(original)
        serialized = []
        expected = {}

        def serialize(payload):
            assert payload == expected
            assert not {"self", "payload", "route"}.intersection(payload)
            serialized.append(json.dumps(payload))
            return serialized[-1]

        async def stop_before_network(session, context, params):
            raise BeforeNetwork

        trace = aiohttp.TraceConfig()
        trace.on_request_start.append(stop_before_network)
        try:
            async with aiohttp.ClientSession(
                json_serialize=serialize, trace_configs=[trace]
            ) as session:

                class FakeHTTP:
                    async def request(self, route, **kwargs):
                        expected.update(kwargs["json"])
                        async with session.request(route.method, route.url, **kwargs):
                            pytest.fail("The request must stop before any network I/O")

                api = BotAPI(FakeHTTP())
                with patch.object(
                    capture,
                    "metadata",
                    side_effect=AssertionError("QQ send must bypass caller scanning"),
                ):
                    with pytest.raises(BeforeNetwork):
                        await getattr(api, method)(content="测试消息", **address)
            assert len(serialized) == 1
            assert json.loads(serialized[0]) == expected
        finally:
            observer.stop()
        assert aiohttp.ClientSession._request is original

    asyncio.run(run())


def test_known_astrbot_frame_still_identifies_event():
    namespace = {
        "__name__": "astrbot.core.pipeline.test_usage_attribution",
        "caller_metadata": caller_metadata,
    }
    exec("def invoke(event):\n    return caller_metadata()\n", namespace)
    result = namespace["invoke"](Event())
    assert result == {
        "kind": "reply",
        "origin": Event.unified_msg_origin,
        "actor": {"id": "qq:test-user", "name": "测试用户"},
        "participants": [{"id": "qq:test-user", "name": "测试用户"}],
    }


def test_scoped_decision_still_keeps_event_attribution():
    async def run():
        module = ModuleType("data.plugins.astrbot_plugin_reply_gate.test_judge")
        module.__dict__["metadata"] = capture.metadata
        exec(
            "async def judge_image(provider, event):\n    return metadata()\n",
            module.__dict__,
        )
        observer = collector()
        original = module.judge_image
        token = BOUND.set(None)
        try:
            observer.wrap_scoped_function(
                module, "judge_image", "decision", event_index=1
            )
            result = await asyncio.wait_for(module.judge_image(None, Event()), 1)
            assert result["kind"] == "decision"
            assert result["origin"] == Event.unified_msg_origin
            assert result["actor"]["id"] == "qq:test-user"
            assert BOUND.get() is None
        finally:
            BOUND.reset(token)
            observer.stop()
        assert module.judge_image is original

    asyncio.run(run())
