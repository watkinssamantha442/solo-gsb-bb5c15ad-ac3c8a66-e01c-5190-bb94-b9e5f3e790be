"""纯函数策略评估引擎（不依赖数据库，便于单测与策略快照对比）。

规则匹配四个维度（AND 关系）：
1. devices   —— 设备，逗号分隔列表；项 "X*" 表示前缀通配；空表示任意；
2. batches   —— 批次，同上；
3. levels    —— 级别，逗号分隔（info/warning/critical），空表示任意；
4. time_window —— 本地时间窗 HH:MM-HH:MM，支持跨零点；空表示任意。

评估语义：
- 规则按 priority 升序逐条检查，第一条四个维度全部命中的规则生效；
- action=pass     ：放行（白名单，优先级高于后面的抑制规则）；
- action=suppress ：抑制；若 cooldown_sec>0，同一冷却键内重复告警只在冷却
                    窗口内被抑制，窗口外的命中告警作为“新事件”放行并重置冷却；
- 没有任何规则命中：默认放行（reason=no_rule）。
"""
from __future__ import annotations

from typing import Any

from .normalize import alert_time_minutes


def _patterns(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def match_field(pattern_raw: str, value: str) -> tuple[bool, str | None]:
    """单维度匹配。返回 (是否命中, 命中的模式说明)。

    - 空模式 = 匹配任意值（"*" 语义）；
    - 模式 "REEFER-*" 表示前缀匹配；
    - 其余模式按精确匹配；可写多个逗号分隔。
    """
    patterns = _patterns(pattern_raw)
    if not patterns:
        return True, None
    text = (value or "").strip()
    for pat in patterns:
        if pat.endswith("*"):
            prefix = pat[:-1]
            if text.startswith(prefix):
                return True, pat
        elif pat == text:
            return True, pat
    return False, None


def _in_window(minutes: int, start: str, end: str) -> bool:
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m < end_m:  # 不跨零点，如 08:00-18:00
        return start_m <= minutes <= end_m
    # 跨零点，如 22:00-06:00
    return minutes >= start_m or minutes <= end_m


def _time_match(window_raw: str, minutes: int | None) -> tuple[bool, dict[str, Any]]:
    window = str(window_raw or "").strip()
    detail: dict[str, Any] = {"window": window or None}
    if not window:
        return True, detail
    if "-" not in window:
        return False, {**detail, "reason": "时间窗格式非法"}
    start, end = window.split("-", 1)
    detail["alert_local_time"] = (
        f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes is not None else None
    )
    if minutes is None:
        # 告警缺少时间，无法验证时间窗 → 视为不命中，避免误抑制
        return False, {**detail, "reason": "告警缺少可解析的时间"}
    ok = _in_window(minutes, start.strip(), end.strip())
    if not ok:
        detail["reason"] = f"告警时间不在 {window} 内"
    return ok, detail


def check_rule(
    rule: dict[str, Any], alert: dict[str, Any], minutes: int | None
) -> tuple[bool, dict[str, Any]]:
    """检查一条规则与一条告警的四个维度是否全部命中，并返回解释细节。"""
    tags: dict[str, str] = {}
    failures: dict[str, str] = {}

    ok, pat = match_field(rule.get("devices", ""), alert.get("device_id", ""))
    if ok:
        tags["devices"] = pat or "*"
    else:
        failures["devices"] = (
            f"设备 {alert.get('device_id', '')!r} 不匹配 {rule.get('devices', '')}"
        )

    ok, pat = match_field(rule.get("batches", ""), alert.get("batch_id", ""))
    if ok:
        tags["batches"] = pat or "*"
    else:
        failures["batches"] = (
            f"批次 {alert.get('batch_id', '') or '(无)'} 不匹配 {rule.get('batches', '')}"
        )

    levels = _patterns(rule.get("levels", ""))
    if not levels:
        tags["levels"] = "*"
    elif alert.get("level", "") in levels:
        tags["levels"] = alert.get("level", "")
    else:
        failures["levels"] = (
            f"级别 {alert.get('level', '')} 不在 {rule.get('levels', '')} 内"
        )

    ok, time_detail = _time_match(rule.get("time_window", ""), minutes)
    if ok:
        if time_detail.get("window"):
            tags["time_window"] = time_detail["window"]
    else:
        failures["time_window"] = time_detail.get("reason", "告警时间不在时间窗内")

    matched = not failures
    return matched, {"tags": tags, "failures": failures}


def _cooldown_key(scope: str, alert: dict[str, Any]) -> str:
    if scope == "device":
        return f"device:{alert.get('device_id', '')}"
    if scope == "global":
        return "global"
    return f"db:{alert.get('device_id', '')}|{alert.get('batch_id', '')}"


def _elapsed_seconds(alert: dict[str, Any], base_alert: dict[str, Any]) -> float | None:
    """两条告警时间之差（秒）；任一缺时间则无法比较。"""
    from datetime import datetime

    def parse(t: str | None) -> datetime | None:
        if not t:
            return None
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None

    a, b = parse(alert.get("alert_time")), parse(base_alert.get("alert_time"))
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def evaluate(rules: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 sequence 顺序评估全部告警，返回每条告警的决定与解释。

    输出列表元素结构：
    {
      "alert_id", "sequence", "verdict": "pass|suppress",
      "rule_id", "rule_name", "reason", "cooldown_seconds"(int|None),
      "explanation": {...}
    }
    """
    enabled_rules = sorted(
        (r for r in rules if int(r.get("enabled", 1)) == 1),
        key=lambda r: (int(r["priority"]), int(r.get("id", 0))),
    )
    ordered_alerts = sorted(alerts, key=lambda a: int(a["sequence"]))

    # 冷却键 -> 最近一条“放行并建立冷却”的抑制类告警
    cd_state: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for alert in ordered_alerts:
        minutes = alert_time_minutes(alert.get("alert_time"))
        checked: list[dict[str, Any]] = []

        for rule in enabled_rules:
            matched, detail = check_rule(rule, alert, minutes)
            checked.append({
                "rule_id": rule["id"],
                "rule_name": rule["name"],
                "priority": rule["priority"],
                "action": rule["action"],
                "matched": matched,
                **detail,
            })
            if not matched:
                continue

            # ===== 第一条命中的规则生效 =====
            if rule["action"] == "pass":
                results.append(_decision(
                    alert, "pass", rule,
                    reason=f"命中放行规则「{rule['name']}」（优先级 {rule['priority']}）",
                    explanation={"hit": _hit_body(rule, detail), "checked": checked},
                ))
                break

            # suppress
            window = int(rule.get("cooldown_sec", 0) or 0)
            scope = rule.get("cooldown_scope", "device_batch") or "device_batch"
            if window <= 0:
                # 纯抑制规则（无冷却窗口）：命中即抑制
                results.append(_decision(
                    alert, "suppress", rule,
                    reason=f"命中抑制规则「{rule['name']}」，该规则无冷却窗口，匹配告警全部抑制",
                    explanation={
                        "hit": _hit_body(rule, detail, {"window_seconds": 0, "scope": scope}),
                        "checked": checked,
                    },
                ))
                break

            # 带冷却窗口的抑制：首个事件放行通知，窗口内重复抑制，窗口外重置
            key = _cooldown_key(scope, alert)
            cd_info: dict[str, Any] = {
                "window_seconds": window,
                "scope": scope,
                "key": key,
            }
            if key in cd_state:
                base = cd_state[key]
                elapsed = _elapsed_seconds(alert, base)
                if elapsed is None:
                    # 缺时间无法判断冷却：按冷却中处理（保守抑制）
                    cd_info.update({
                        "within_cooldown": True,
                        "remaining_seconds": None,
                        "base_alert_sequence": base["sequence"],
                        "note": "告警缺少时间，按冷却中保守抑制",
                    })
                    results.append(_decision(
                        alert, "suppress", rule,
                        reason=(
                            f"命中抑制规则「{rule['name']}」，处于设备/批次冷却窗口内"
                            f"（基准 #{base['sequence']}）"
                        ),
                        cooldown_seconds=window,
                        explanation={"hit": _hit_body(rule, detail, cd_info), "checked": checked},
                    ))
                    break
                if elapsed < 0:
                    # 乱序到达（告警时间早于当前冷却基准）：不重置冷却，保守抑制
                    cd_info.update({
                        "within_cooldown": True,
                        "elapsed_seconds": int(elapsed),
                        "remaining_seconds": None,
                        "base_alert_sequence": base["sequence"],
                        "note": "告警时间早于冷却基准（乱序到达），不重置冷却",
                    })
                    results.append(_decision(
                        alert, "suppress", rule,
                        reason=(
                            f"命中抑制规则「{rule['name']}」，告警时间早于冷却基准"
                            f" #{base['sequence']}（乱序到达），按冷却中抑制"
                        ),
                        cooldown_seconds=window,
                        explanation={"hit": _hit_body(rule, detail, cd_info), "checked": checked},
                    ))
                    break
                if elapsed < window:
                    remaining = int(window - elapsed)
                    cd_info.update({
                        "within_cooldown": True,
                        "elapsed_seconds": int(elapsed),
                        "remaining_seconds": remaining,
                        "base_alert_sequence": base["sequence"],
                    })
                    results.append(_decision(
                        alert, "suppress", rule,
                        reason=(
                            f"命中抑制规则「{rule['name']}」，冷却窗口内重复告警"
                            f"（{int(elapsed)}s < {window}s，剩余 {remaining}s，基准 #{base['sequence']}）"
                        ),
                        cooldown_seconds=remaining,
                        explanation={"hit": _hit_body(rule, detail, cd_info), "checked": checked},
                    ))
                    break
                # 冷却已过：落入下方“新抑制事件”路径
                cd_info.update({
                    "within_cooldown": False,
                    "elapsed_seconds": int(elapsed),
                    "previous_alert_sequence": base["sequence"],
                })

            # 新抑制事件：放行通知 + 建立/重置冷却
            cd_state[key] = alert
            cd_info.setdefault("within_cooldown", False)
            cd_info["reset_by"] = alert["sequence"]
            results.append(_decision(
                alert, "pass", rule,
                reason=(
                    f"命中抑制规则「{rule['name']}」，首次通知并放行"
                    f"，开启 {window}s 冷却（维度：{_scope_label(scope)}）"
                ),
                cooldown_seconds=None,
                explanation={"hit": _hit_body(rule, detail, cd_info), "checked": checked},
            ))
            break
        else:
            # 没有任何启用规则命中
            results.append({
                "alert_id": alert["id"],
                "sequence": alert["sequence"],
                "verdict": "pass",
                "rule_id": None,
                "rule_name": "",
                "reason": "未命中任何规则，按默认策略放行",
                "cooldown_seconds": None,
                "explanation": {"no_rule": True, "checked": checked},
            })

    return results


def _scope_label(scope: str) -> str:
    return {"device_batch": "同设备+同批次", "device": "同设备", "global": "全局"}.get(
        scope, "同设备+同批次"
    )


def _hit_body(
    rule: dict[str, Any],
    detail: dict[str, Any],
    cd_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "priority": rule["priority"],
        "action": rule["action"],
        "matched_dimensions": detail["tags"],
    }
    if cd_info is not None:
        body["cooldown"] = cd_info
    return body


def _decision(
    alert: dict[str, Any],
    verdict: str,
    rule: dict[str, Any],
    reason: str,
    explanation: dict[str, Any],
    cooldown_seconds: int | None = None,
) -> dict[str, Any]:
    return {
        "alert_id": alert["id"],
        "sequence": alert["sequence"],
        "verdict": verdict,
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "reason": reason,
        "cooldown_seconds": cooldown_seconds,
        "explanation": explanation,
    }
