"""告警与规则字段的归一化：级别别名、时间解析、文本规整。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# 级别标准值与中英文别名
LEVEL_ALIASES: dict[str, str] = {
    "info": "info",
    "information": "info",
    "notice": "info",
    "提示": "info",
    "信息": "info",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "告警": "warning",
    "警告": "warning",
    "critical": "critical",
    "fatal": "critical",
    "error": "critical",
    "emergency": "critical",
    "严重": "critical",
    "致命": "critical",
    "紧急": "critical",
}

# 数值严重度（导入告警里若给数字时使用）
SEVERITY_NUMBER = {1: "critical", 2: "warning", 3: "info"}

# 规则编辑器允许出现的级别
VALID_LEVELS = {"info", "warning", "critical"}


def canonical_level(value: Any) -> str:
    """把任意形态的级别归一为 info / warning / critical，未知值保留小写原样。"""
    if isinstance(value, bool):
        return "info"
    if isinstance(value, (int, float)):
        return SEVERITY_NUMBER.get(int(value), str(int(value)).lower())
    text = str(value).strip().lower() if value is not None else ""
    return LEVEL_ALIASES.get(text, text or "info")


def normalize_levels_csv(raw: str) -> str:
    """规则表单中的级别字段：去空白、归一化、去重、按标准顺序输出。"""
    tokens = [canonical_level(t) for t in str(raw or "").split(",") if t.strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    order = {"critical": 0, "warning": 1, "info": 2}
    ordered.sort(key=lambda t: order.get(t, 99))
    return ",".join(ordered)


def parse_datetime(value: Any) -> str | None:
    """尽量宽松地解析告警时间，返回带时区的 ISO8601 字符串；无法解析返回 None。

    支持：
    - 数字（epoch 秒 / 毫秒）；
    - ISO8601（含末尾 Z）；
    - "YYYY-MM-DD HH:MM:SS" 等常见格式（按本地时区处理）。
    """
    if value is None or value == "":
        return None
    # epoch
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e12:  # 毫秒时间戳
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return None
    # ISO8601：把末尾 Z 换成 +00:00
    iso_text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.isoformat(timespec="seconds")


_HHMM = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]?\d)\s*$")


def validate_time_window(raw: str) -> str:
    """校验并归一化 HH:MM-HH:MM；空字符串表示不限制。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if "-" not in text:
        raise ValueError("时间窗格式应为 HH:MM-HH:MM，例如 22:00-06:00")
    start_s, end_s = text.split("-", 1)
    m1, m2 = _HHMM.match(start_s), _HHMM.match(end_s)
    if not m1 or not m2:
        raise ValueError("时间窗格式应为 HH:MM-HH:MM（24 小时制）")
    start = f"{int(m1.group(1)):02d}:{int(m1.group(2)):02d}"
    end = f"{int(m2.group(1)):02d}:{int(m2.group(2)):02d}"
    if start == end:
        raise ValueError("时间窗起止时间不能相同（如需全天匹配请留空）")
    return f"{start}-{end}"


def alert_time_minutes(iso_text: str | None) -> int | None:
    """从 ISO8601 告警时间中取出分钟数 HH*60+MM。

    匹配规则（对沙盒最直观、且不依赖部署机器时区）：
    - 带时区偏移的时间戳：直接用时间戳里书写的当地钟点
      （"2026-09-21T23:30+08:00" → 23:30；"…T23:30Z" → UTC 的 23:30）；
    - 不带时区的朴素时间戳：按字面钟点处理。
    """
    if not iso_text:
        return None
    try:
        dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute
