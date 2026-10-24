"""AstrBot usage dashboard; all metrics remain local and outside chat history."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from sys import maxsize
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core import db_helper
from astrbot.core.db.po import ProviderStat
from sqlmodel import select

from .balances import BalanceMonitor
from .capture import Collector
from .ledger import KINDS, Ledger, prices_from_json
from .pricing import PricingCatalog
from .usage_tool import query_usage


class UsageInsight(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        directory = StarTools.get_data_dir("astrbot_plugin_usage_insight")
        directory.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(directory / "usage.sqlite3")
        self.pricing = PricingCatalog(directory)
        self.pricing.bind_providers(self.context.get_all_providers())
        self.collector = Collector(
            self.ledger,
            lambda: self.config.get("enabled", True),
            self.logger,
            price_snapshot=self.pricing.resolve,
        )
        self._import_task = None
        self._pricing_task = None
        self.balances = BalanceMonitor(context, self.ledger, config, self.pricing)
        self.collector.balance_scope = self.balances.credential_ref
        self._balance_task = None
        self.import_status = "pending"
        # AstrBot 4.29.1 keeps registered APIs across plugin reloads.
        apis = getattr(context, "registered_web_apis", None)
        if isinstance(apis, list):
            retired = {
                "/astrbot_plugin_usage_insight/official_preview",
                "/astrbot_plugin_usage_insight/official_reconcile",
            }
            apis[:] = [api for api in apis if api[0] not in retired]
        for path, handler, methods in (
            ("report", self.api_report, ["GET"]),
            ("prices", self.api_prices, ["GET", "POST"]),
            ("refresh_prices", self.api_refresh_prices, ["POST"]),
            ("balances", self.api_balances, ["GET", "POST"]),
        ):
            context.register_web_api(
                "/astrbot_plugin_usage_insight/" + path,
                handler,
                methods,
                "Usage and cost estimates",
            )

    async def initialize(self):
        self.collector.refresh(self.context.get_all_providers())
        self._import_task = asyncio.create_task(self.import_history())
        self._pricing_task = asyncio.create_task(self.pricing.run())
        self._balance_task = asyncio.create_task(self.balances.run())

    @filter.llm_tool(name="get_self_balance")
    async def get_self_balance(self, event: AstrMessageEvent) -> str:
        """查询当前会话账户的人民币可用余额，单位元。

        成功只返回金额数值，例如 12.34；无法查询人民币余额时返回“未知”。
        """
        try:
            selected = event.get_extra("selected_provider")
            if isinstance(selected, str) and selected:
                provider = self.context.get_provider_by_id(selected)
            else:
                provider = await self.context.get_using_provider_async(
                    umo=event.unified_msg_origin
                )
            result = await self.balances.query(provider)
            if result.get("ok"):
                for entry in result.get("balance_infos", []):
                    if entry.get("currency") == "CNY":
                        return entry["total_balance"]
        except Exception:
            pass
        return "未知"

    @filter.llm_tool(name="get_usage_stats")
    async def get_usage_stats(
        self,
        event: AstrMessageEvent,
        days: int = 7,
    ) -> str:
        """查询最近的总 token、总人民币花费和全部成员的花费排行。

        金额为人民币估算，单位元；个人按花费降序排列，均含 token 和花费。
        总额包含后台调用，已分类的上下文压缩开销另列。数据不完整的数值返回 null。

        Args:
            days(number): 整数天数，默认 7；1 为今天、30 为近 30 天、0 为全部已采集数据，范围 0～36500。
        """
        try:
            self.pricing.bind_providers(self.context.get_all_providers())
            result = await asyncio.to_thread(
                query_usage,
                self.ledger,
                prices_from_json(self.config.get("prices_json", "[]")),
                self.pricing,
                days=days,
                timezone=self._timezone(),
            )
        except ValueError as exc:
            result = {"ok": False, "error": "invalid_query", "message": str(exc)}
        except Exception:
            result = {
                "ok": False,
                "error": "stats_unavailable",
                "message": "本地统计暂时不可用，请稍后重试",
            }
        return json.dumps(result, ensure_ascii=False)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def bind_message(self, event: AstrMessageEvent):
        self.collector.bind_event(event)

    @filter.on_llm_request(priority=maxsize)
    async def bind_request(self, event: AstrMessageEvent, req):
        self.collector.bind_event(event)
        self.collector.refresh(self.context.get_all_providers())

    @filter.on_plugin_loaded()
    async def refresh_integrations(self, plugin):
        self.collector.refresh(self.context.get_all_providers())

    async def import_history(self):
        """Import only pre-installation host stats; a host row is one agent turn."""
        try:
            cutoff = datetime.fromtimestamp(self.ledger.started, timezone.utc).replace(
                tzinfo=None
            )
            days = max(1, min(36500, int(self.config.get("history_days", 365))))
            begin = datetime.fromtimestamp(
                self.ledger.started - days * 86400, timezone.utc
            ).replace(tzinfo=None)
            offset = 0
            while True:
                async with db_helper.get_db() as session:
                    records = (
                        (
                            await session.execute(
                                select(ProviderStat)
                                .where(
                                    ProviderStat.created_at < cutoff,
                                    ProviderStat.created_at >= begin,
                                )
                                .order_by(ProviderStat.id)
                                .offset(offset)
                                .limit(500)
                            )
                        )
                        .scalars()
                        .all()
                    )
                for record in records:
                    date = record.created_at
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    input_count = record.token_input_other + record.token_input_cached
                    # Historical zero cannot establish whether cache reporting
                    # was supported, or whether a failed call was actually free.
                    usage = (
                        {
                            "input": input_count,
                            "cached": record.token_input_cached
                            if record.token_input_cached > 0
                            else None,
                            "output": record.token_output,
                        }
                        if input_count or record.token_output
                        else None
                    )
                    row = {
                        "ts": record.start_time
                        if record.start_time > 0
                        else date.timestamp(),
                        "provider": record.provider_id or "unknown",
                        "model": record.provider_model or "unknown",
                        "kind": "history",
                        "origin": record.umo or "",
                        "actor": None,
                        "participants": [],
                        "unit": "turn",
                        "status": record.status,
                        "duration": max(0, record.end_time - record.start_time),
                        "usage": usage,
                        "error": "",
                    }
                    self.ledger.put(
                        row,
                        "native:" + str(record.id),
                        commit=False,
                        update_history=True,
                    )
                self.ledger.db.commit()
                if len(records) < 500:
                    break
                offset += len(records)
                await asyncio.sleep(0)
            self.import_status = "complete"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.import_status = "failed"
            self.logger.warning("历史模型用量导入失败：%s", type(exc).__name__)

    def _timezone(self):
        value = (
            self.context.get_config().get("timezone", "Asia/Shanghai")
            or "Asia/Shanghai"
        )
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return "Asia/Shanghai"

    async def api_report(self):
        try:
            self.pricing.bind_providers(self.context.get_all_providers())
            rules = prices_from_json(self.config.get("prices_json", "[]"))
            query = request.query
            result = await asyncio.to_thread(
                self.ledger.report,
                rules,
                days=int(query.get("days", 7)),
                timezone=self._timezone(),
                provider=str(query.get("provider", "")),
                model=str(query.get("model", "")),
                kind=str(query.get("kind", "")),
                origin=str(query.get("origin", "")),
                actor=str(query.get("actor", "")),
                attribution=str(query.get("attribution", "trigger")),
                offset=int(query.get("offset", 0)),
                limit=int(query.get("limit", 50)),
                catalog=self.pricing,
            )
            result.update(
                import_status=self.import_status,
                kind_labels=KINDS,
                collecting=bool(self.config.get("enabled", True)),
                generated_at=time.time(),
                auto_pricing=self.pricing.status(),
            )
            return json_response(result)
        except (ValueError, TypeError, json.JSONDecodeError):
            return error_response(
                "查询参数或价格规则无效，请检查插件配置", status_code=400
            )

    async def api_prices(self):
        try:
            if request.method == "POST":
                payload = await request.json()
                if not isinstance(payload, dict) or set(payload) != {"prices"}:
                    raise ValueError("请提交 prices 数组")
                rules = prices_from_json(payload["prices"])
                old = self.config.get("prices_json", "[]")
                self.config["prices_json"] = json.dumps(rules, ensure_ascii=False)
                try:
                    self.config.save_config()
                except Exception:
                    self.config["prices_json"] = old
                    raise
            else:
                rules = prices_from_json(self.config.get("prices_json", "[]"))
            return json_response(
                {"prices": rules, "auto_pricing": self.pricing.status()}
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("价格保存失败，请重试", status_code=500)

    async def api_refresh_prices(self):
        success = await self.pricing.refresh()
        return json_response({"synced": success, "auto_pricing": self.pricing.status()})

    async def api_balances(self):
        try:
            if request.method == "POST":
                await self.balances.refresh()
            rules = prices_from_json(self.config.get("prices_json", "[]"))
            return json_response(
                await self.balances.view(
                    rules,
                    days=int(request.query.get("days", 7)),
                    timezone=self._timezone(),
                )
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            return error_response("价格规则无效，请检查插件配置", status_code=400)

    async def terminate(self):
        if self._balance_task is not None:
            self._balance_task.cancel()
            try:
                await self._balance_task
            except asyncio.CancelledError:
                pass
        if self._pricing_task is not None:
            self._pricing_task.cancel()
            try:
                await self._pricing_task
            except asyncio.CancelledError:
                pass
        if self._import_task is not None and not self._import_task.done():
            self._import_task.cancel()
            try:
                await self._import_task
            except asyncio.CancelledError:
                pass
        self.collector.stop()
