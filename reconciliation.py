"""Compare imported per-key official records with independently collected usage."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import re
import time
import zipfile
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ledger import estimate
from .pricing import ALIASES

MAX_BYTES = 4 * 1024 * 1024
FIELDS = ("date", "key", "model", "input", "cached", "output", "amount", "currency")


def display(value):
    text = str(value)
    text = re.sub(r"sk[-_][A-Za-z0-9_-]{8,}", "[已隐藏的 API Key]", text)
    return text[:180]


def sources(payload):
    try:
        encoded = payload["content"]
        if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
            raise ValueError("文件不能超过 4 MB")
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, binascii.Error, TypeError):
        raise ValueError("文件内容无效，请重新选择 CSV 或 ZIP") from None
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("文件为空或超过 4 MB")
    files = []
    if zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.infolist()) > 20:
                    raise ValueError("压缩包文件过多")
                total = 0
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".csv"):
                        continue
                    total += info.file_size
                    if (
                        info.flag_bits & 1
                        or total > MAX_BYTES * 4
                        or info.file_size > MAX_BYTES
                    ):
                        raise ValueError("CSV 太大或压缩包需要密码")
                    with archive.open(info) as stream:
                        body = stream.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError("CSV 超过 4 MB")
                    files.append((info.filename, body))
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise ValueError("无法读取 ZIP，请直接选择其中的 CSV") from None
    else:
        files.append((str(payload.get("filename", "official.csv")), raw))
    if not files:
        raise ValueError("没有找到 CSV 文件")
    result = []
    for name, body in files:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = body.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("CSV 编码无法识别，请使用 UTF-8 或 GB18030")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
            reader = csv.reader(io.StringIO(text), dialect=dialect, strict=True)
            header = next(reader)
            if not 1 <= len(header) <= 80:
                raise ValueError("CSV 列数无效")
            rows = []
            for row in reader:
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) != len(header) or any(len(cell) > 4096 for cell in row):
                    raise ValueError("CSV 行列不齐或单元格过长")
                rows.append(row)
                if len(rows) > 50000:
                    raise ValueError("单个 CSV 最多支持 50000 行")
        except (csv.Error, StopIteration):
            raise ValueError("CSV 格式无效，请选择带表头的导出文件") from None
        if not rows:
            raise ValueError("CSV 没有明细行")
        result.append(
            {
                "name": display(PurePosixPath(name.replace("\\", "/")).name),
                "header": header,
                "rows": rows,
            }
        )
    return result, hashlib.sha256(raw).hexdigest()


def key_values(table, column):
    values = list(
        dict.fromkeys(
            row[column].strip() for row in table["rows"] if row[column].strip()
        )
    )
    if len(values) > 200:
        raise ValueError("Key 列有超过 200 个不同值，请确认所选列")
    return values


def preview(payload):
    tables, _ = sources(payload)
    result = {
        "files": [
            {
                "index": i,
                "name": t["name"],
                "headers": [display(h) for h in t["header"]],
                "rows": len(t["rows"]),
                "sample": [[display(c) for c in row] for row in t["rows"][:4]],
            }
            for i, t in enumerate(tables)
        ]
    }
    if "key_column" in payload:
        index, column = payload.get("file_index"), payload["key_column"]
        if type(index) is not int or not 0 <= index < len(tables):
            raise ValueError("请选择 CSV 文件")
        if type(column) is not int or not 0 <= column < len(tables[index]["header"]):
            raise ValueError("Key 列无效")
        result["keys"] = [
            {"index": i, "label": display(v)}
            for i, v in enumerate(key_values(tables[index], column))
        ]
    return result


def number(value, *, integer=False):
    try:
        text = value.strip()
        if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            text = text.replace(",", "")
        amount = Decimal(text)
        if (
            not amount.is_finite()
            or abs(amount) > Decimal("1e15")
            or len(text) > 64
            or not -12 <= amount.as_tuple().exponent <= 15
        ):
            raise ValueError()
        if integer and (amount < 0 or amount != int(amount)):
            raise ValueError()
        return int(amount) if integer else amount
    except (InvalidOperation, ValueError):
        raise ValueError("数值无效") from None


def timestamp(value, timezone):
    text = value.strip()
    try:
        if re.fullmatch(r"\d{10}|\d{13}", text):
            return float(text) / (1000 if len(text) == 13 else 1)
        if re.fullmatch(r"\d{8}", text):
            date = datetime.strptime(text, "%Y%m%d")
        else:
            date = datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZoneInfo(timezone))
        return date.timestamp()
    except (ValueError, OverflowError):
        raise ValueError("时间无法识别，请使用日期、ISO 时间或 Unix 时间戳") from None


def bucket():
    return {
        "official_rows": 0,
        "local_records": 0,
        "official_input": 0,
        "official_cached": 0,
        "official_output": 0,
        "local_input": 0,
        "local_cached": 0,
        "local_output": 0,
        "official_cost": {},
        "local_cost": {},
        "quantum": {},
        "usage_unknown": 0,
        "cache_unknown": 0,
        "cost_unknown": 0,
    }


def amount_matches(local, actual, quantum):
    with localcontext() as context:
        context.prec = 50
        return local.quantize(quantum, rounding=ROUND_HALF_UP) == actual


def compare(payload, ledger, credential_ref, providers, salt, rules, catalog, timezone):
    tables, digest = sources(payload)
    file_index = payload.get("file_index")
    if type(file_index) is not int or not 0 <= file_index < len(tables):
        raise ValueError("请选择 CSV 文件")
    table, mapping = tables[file_index], payload.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) != set(FIELDS):
        raise ValueError("请确认列映射")
    for field, column in mapping.items():
        if type(column) is not int or not -1 <= column < len(table["header"]):
            raise ValueError("列映射无效")
    mapped = [column for column in mapping.values() if column >= 0]
    if len(mapped) != len(set(mapped)):
        raise ValueError("不同字段不能使用同一列")
    if mapping["date"] < 0:
        raise ValueError("需要选择明细时间列")
    measures = [f for f in ("input", "cached", "output", "amount") if mapping[f] >= 0]
    if not measures:
        raise ValueError("至少映射一项用量或扣费金额")
    input_mode = payload.get("input_mode", "total")
    if (
        input_mode not in ("total", "miss")
        or input_mode == "miss"
        and mapping["input"] >= 0
        and mapping["cached"] < 0
    ):
        raise ValueError("输入不含缓存时，还需映射缓存输入列")
    csv_timezone = str(payload.get("csv_timezone", timezone))
    try:
        ZoneInfo(csv_timezone)
        start = datetime.strptime(payload["start"], "%Y-%m-%d").replace(
            tzinfo=ZoneInfo(timezone)
        )
        end = (
            datetime.strptime(payload["end"], "%Y-%m-%d") + timedelta(days=1)
        ).replace(tzinfo=ZoneInfo(timezone))
        if not 0 < (end - start).days <= 36500:
            raise ValueError()
    except (KeyError, ValueError, OverflowError, TypeError, ZoneInfoNotFoundError):
        raise ValueError("核对起止日期无效") from None
    start_ts, end_ts = start.timestamp(), end.timestamp()
    selected_key = None
    binding = "file_filtered_by_user"
    if mapping["key"] >= 0:
        values = key_values(table, mapping["key"])
        index = payload.get("key_value_index")
        if type(index) is not int or not 0 <= index < len(values):
            raise ValueError("请选择官方明细中对应当前 Key 的值")
        selected_key = values[index]
        if (
            selected_key.startswith(("sk-", "sk_"))
            and "*" not in selected_key
            and "…" not in selected_key
            and len(selected_key) >= 24
        ):
            actual = hmac.new(salt, selected_key.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(actual, credential_ref):
                raise ValueError("明细中的完整 API Key 与当前提供商不一致")
            binding = "full_key_matched"
        else:
            binding = "official_key_value_selected_by_user"
    elif payload.get("file_is_current_key") is not True:
        raise ValueError("没有 Key 列时，请确认此文件已经只导出当前 Key")
    default_currency = payload.get("currency", "CNY")
    if default_currency not in ("CNY", "USD"):
        raise ValueError("请选择 CNY 或 USD")
    groups, selected_rows, other_keys, outside = {}, 0, 0, 0
    for i, row in enumerate(table["rows"], 2):
        if selected_key is not None and row[mapping["key"]].strip() != selected_key:
            other_keys += 1
            continue
        try:
            ts = timestamp(row[mapping["date"]], csv_timezone)
            if not start_ts <= ts < end_ts:
                outside += 1
                continue
            model = (
                row[mapping["model"]].strip() if mapping["model"] >= 0 else "全部模型"
            )
            if not model:
                raise ValueError("模型名为空")
            model = ALIASES.get(model, model)
            group = groups.setdefault(model, bucket())
            group["official_rows"] += 1
            selected_rows += 1
            if (
                input_mode == "total"
                and mapping["input"] >= 0
                and mapping["cached"] >= 0
                and number(row[mapping["cached"]], integer=True)
                > number(row[mapping["input"]], integer=True)
            ):
                raise ValueError("缓存输入不能大于总输入，请检查输入列口径")
            for field in ("input", "cached", "output"):
                if mapping[field] >= 0:
                    group["official_" + field] += number(
                        row[mapping[field]], integer=True
                    )
            if input_mode == "miss" and mapping["input"] >= 0:
                group["official_input"] += number(row[mapping["cached"]], integer=True)
            if mapping["amount"] >= 0:
                currency = (
                    row[mapping["currency"]].strip().upper()
                    if mapping["currency"] >= 0
                    else default_currency
                )
                if currency not in ("CNY", "USD"):
                    raise ValueError("币种不是 CNY 或 USD")
                amount = number(row[mapping["amount"]])
                group["official_cost"][currency] = (
                    group["official_cost"].get(currency, Decimal(0)) + amount
                )
                quantum = Decimal(1).scaleb(amount.as_tuple().exponent)
                group["quantum"][currency] = min(
                    group["quantum"].get(currency, quantum), quantum
                )
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"CSV 第 {i} 行：{exc}") from None
    if not selected_rows:
        raise ValueError("所选 Key 和日期范围没有官方明细")
    untagged, boundary = 0, 0
    for (data,) in ledger.read_rows(
        "SELECT data FROM receipts WHERE ts<? AND (ts>=? OR ts+CAST(json_extract(data,'$.duration') AS REAL)>=?)",
        (end_ts, start_ts, start_ts),
    ):
        row = json.loads(data)
        if row.get("balance_ref") != credential_ref:
            if not row.get("balance_ref") and row["provider"] in providers:
                untagged += 1
            continue
        if row["ts"] < start_ts or row["ts"] + row["duration"] >= end_ts:
            boundary += 1
        if row["ts"] < start_ts:
            continue
        model = (
            ALIASES.get(row["model"], row["model"])
            if mapping["model"] >= 0
            else "全部模型"
        )
        group = groups.setdefault(model, bucket())
        group["local_records"] += 1
        usage = row.get("usage")
        if usage is None:
            group["usage_unknown"] += 1
        else:
            for field in ("input", "output"):
                group["local_" + field] += usage[field]
            if usage["cached"] is None:
                group["cache_unknown"] += 1
            else:
                group["local_cached"] += usage["cached"]
        cost = estimate(row, rules, catalog)
        if cost is None:
            group["cost_unknown"] += 1
        else:
            currency = cost["currency"]
            group["local_cost"][currency] = group["local_cost"].get(
                currency, Decimal(0)
            ) + Decimal(str(cost["total"]))
    notes = []
    if start_ts < ledger.started:
        notes.append("核对起始时间早于详细采集开始，无法证明此前的 Key 用量完整")
    if untagged:
        notes.append(f"{untagged} 条同提供商记录缺少 Key 标记，未纳入 Key 核对")
    if boundary:
        notes.append(f"{boundary} 条调用跨越日期边界，官方时间口径可能不同")
    if end_ts > time.time():
        notes.append("核对区间尚未结束，官方明细可能继续更新")
    results, states = [], []
    for model, group in sorted(groups.items()):
        checks = []
        for field in ("input", "cached", "output"):
            if mapping[field] < 0:
                checks.append(
                    {
                        "metric": field,
                        "state": "unverified",
                        "official": None,
                        "local": group["local_" + field],
                        "difference": None,
                    }
                )
                continue
            actual, local = group["official_" + field], group["local_" + field]
            incomplete = bool(
                notes
                or group["usage_unknown"]
                or field == "cached"
                and group["cache_unknown"]
            )
            state = (
                "incomplete"
                if incomplete
                else "matched"
                if actual == local
                else "different"
            )
            checks.append(
                {
                    "metric": field,
                    "state": state,
                    "official": actual,
                    "local": local,
                    "difference": local - actual,
                }
            )
            states.append(state)
        amounts = []
        if mapping["amount"] >= 0:
            currencies = set(group["official_cost"]) | set(group["local_cost"])
            for currency in sorted(currencies):
                actual = group["official_cost"].get(currency, Decimal(0))
                local = group["local_cost"].get(currency, Decimal(0))
                quantum = group["quantum"].get(currency)
                incomplete = bool(notes or group["cost_unknown"] or quantum is None)
                state = (
                    "incomplete"
                    if incomplete
                    else "matched"
                    if amount_matches(local, actual, quantum)
                    else "different"
                )
                amounts.append(
                    {
                        "currency": currency,
                        "official": str(actual),
                        "local": str(local),
                        "difference": str(local - actual),
                        "state": state,
                        "precision": str(quantum) if quantum else None,
                    }
                )
                states.append(state)
        results.append(
            {
                "model": display(model),
                "official_rows": group["official_rows"],
                "local_records": group["local_records"],
                "usage_unknown": group["usage_unknown"],
                "cache_unknown": group["cache_unknown"],
                "cost_unknown": group["cost_unknown"],
                "usage": checks,
                "amounts": amounts,
            }
        )
    state = (
        "different"
        if "different" in states
        else "incomplete"
        if "incomplete" in states
        else "matched"
        if states
        else "unverified"
    )
    if state == "matched" and not any(
        g["local_records"]
        or g["official_input"]
        or g["official_cached"]
        or g["official_output"]
        or any(g["official_cost"].values())
        for g in groups.values()
    ):
        state = "no_activity"
    identity = json.dumps(
        {k: v for k, v in payload.items() if k not in ("content", "filename")},
        sort_keys=True,
        ensure_ascii=False,
    )
    identifier = hashlib.sha256(
        (digest + credential_ref + identity).encode()
    ).hexdigest()
    return {
        "id": identifier,
        "checked_at": time.time(),
        "source_name": table["name"],
        "file_sha256": digest,
        "provider_ids": [display(p) for p in providers],
        "key_tag": credential_ref[:12],
        "key_binding": binding,
        "key_label": display(selected_key)
        if selected_key is not None
        else "用户确认文件已筛选当前 Key",
        "start": payload["start"],
        "end": payload["end"],
        "timezone": timezone,
        "csv_timezone": csv_timezone,
        "selected_rows": selected_rows,
        "other_key_rows": other_keys,
        "outside_rows": outside,
        "untagged_records": untagged,
        "boundary_records": boundary,
        "state": state,
        "amount_available": mapping["amount"] >= 0,
        "notes": notes,
        "models": results,
    }


class Reconciliations:
    def __init__(self, ledger):
        self.ledger = ledger
        ledger.db.execute(
            "CREATE TABLE IF NOT EXISTS reconciliations(id TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT NOT NULL)"
        )
        ledger.db.commit()

    def save(self, result):
        self.ledger.db.execute(
            "INSERT INTO reconciliations VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts,data=excluded.data",
            (
                result["id"],
                result["checked_at"],
                json.dumps(result, ensure_ascii=False),
            ),
        )
        self.ledger.db.commit()

    def recent(self):
        return [
            json.loads(data)
            for (data,) in self.ledger.read_rows(
                "SELECT data FROM reconciliations ORDER BY ts DESC LIMIT 20", ()
            )
        ]
