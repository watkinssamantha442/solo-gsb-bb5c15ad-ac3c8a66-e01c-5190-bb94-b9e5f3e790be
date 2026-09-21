"""服务层：告警归一化、策略评估落库、注入/撤销、快照对比、模拟接收端、演示数据。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import engine
from .db import (
    execute,
    get_conn,
    get_lock,
    now_iso,
    query_all,
    query_one,
    transaction,
)
from .normalize import canonical_level, parse_datetime

DEVICE_KEYS = ("device_id", "deviceId", "device", "device_code", "deviceCode", "设备")
BATCH_KEYS = ("batch_id", "batchId", "batch", "lot", "lot_id", "批次")
LEVEL_KEYS = ("level", "severity", "level_name", "levelName", "级别")
TYPE_KEYS = ("alert_type", "alertType", "type", "event", "code", "告警类型")
MESSAGE_KEYS = ("message", "msg", "detail", "description", "desc", "描述", "消息")
TIME_KEYS = ("alert_time", "alertTime", "time", "timestamp", "occurred_at",
             "occurredAt", "ts", "datetime", "时间")


def _first(raw: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return ""


def normalize_alert(raw: Any) -> dict[str, Any]:
    """把一条宽松结构的原始告警归一为内部结构；device_id 必填。"""
    if not isinstance(raw, dict):
        raise ValueError("告警必须是 JSON 对象")
    device_id = str(_first(raw, DEVICE_KEYS)).strip()
    if not device_id:
        raise ValueError("缺少设备标识字段（device_id / device / deviceId）")
    batch_id = str(_first(raw, BATCH_KEYS)).strip()
    level = canonical_level(_first(raw, LEVEL_KEYS) or "info")
    return {
        "device_id": device_id,
        "batch_id": batch_id,
        "level": level,
        "alert_type": str(_first(raw, TYPE_KEYS)).strip(),
        "message": str(_first(raw, MESSAGE_KEYS)).strip(),
        "alert_time": parse_datetime(_first(raw, TIME_KEYS)),
        "payload_json": json.dumps(raw, ensure_ascii=False),
    }


# ---------------------------------------------------------------- 读取

def list_rules(include_disabled: bool = True) -> list[dict]:
    rows = query_all("SELECT * FROM rules ORDER BY priority ASC, id ASC")
    return [dict(r) for r in rows]


def list_alerts() -> list[dict]:
    rows = query_all("SELECT * FROM alerts ORDER BY sequence ASC")
    return [dict(r) for r in rows]


def list_decisions() -> list[dict]:
    rows = query_all(
        """
        SELECT d.*, a.device_id, a.batch_id, a.level, a.alert_type, a.message,
               a.alert_time, a.payload_json
        FROM decisions d JOIN alerts a ON a.id = d.alert_id
        ORDER BY d.sequence ASC
        """
    )
    out = []
    for r in rows:
        item = dict(r)
        item["explanation"] = json.loads(item.pop("explanation_json"))
        out.append(item)
    return out


def overview() -> dict:
    d = list_decisions()
    passed = sum(1 for x in d if x["verdict"] == "pass")
    suppressed = len(d) - passed
    return {
        "alerts_total": len(d),
        "pass": passed,
        "suppress": suppressed,
        "rules_total": len(list_rules()),
        "auto_forward": get_setting("auto_forward") == "1",
        "inbox_total": query_one("SELECT COUNT(*) c FROM receiver_events")["c"],
    }


# ---------------------------------------------------------------- 评估落库

def _rules_for_engine() -> list[dict]:
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "enabled": r["enabled"],
            "priority": r["priority"],
            "action": r["action"],
            "devices": r["devices"],
            "batches": r["batches"],
            "levels": r["levels"],
            "time_window": r["time_window"],
            "cooldown_sec": r["cooldown_sec"],
            "cooldown_scope": r["cooldown_scope"],
        }
        for r in query_all("SELECT * FROM rules ORDER BY priority ASC, id ASC")
    ]


def _alerts_for_engine() -> list[dict]:
    return [dict(r) for r in query_all("SELECT * FROM alerts ORDER BY sequence ASC")]


def recompute() -> list[dict]:
    """用当前规则重算全部告警，并整体替换 decisions 表（持锁保证原子）。"""
    with get_lock():
        rules = _rules_for_engine()
        alerts = _alerts_for_engine()
        decisions = engine.evaluate(rules, alerts)
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM decisions")
            stamp = now_iso()
            conn.executemany(
                """INSERT INTO decisions(alert_id, sequence, verdict, rule_id,
                       rule_name, reason, cooldown_seconds, explanation_json, evaluated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        d["alert_id"], d["sequence"], d["verdict"],
                        d["rule_id"], d["rule_name"], d["reason"],
                        d["cooldown_seconds"],
                        json.dumps(d["explanation"], ensure_ascii=False),
                        stamp,
                    )
                    for d in decisions
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return decisions


# ---------------------------------------------------------------- 模拟接收端

def _already_forwarded(alert_ids: list[int]) -> set[int]:
    if not alert_ids:
        return set()
    marks = query_all(
        f"SELECT DISTINCT alert_id FROM receiver_events "
        f"WHERE source='forward' AND alert_id IN ({','.join('?' * len(alert_ids))})",
        tuple(alert_ids),
    )
    return {r["alert_id"] for r in marks if r["alert_id"] is not None}


def _record_forward(decisions: list[dict]) -> list[dict]:
    """把给定 PASS 决定投递到模拟接收端（已投递过的自动去重）。"""
    targets = [d for d in decisions if d["verdict"] == "pass"]
    already = _already_forwarded([d["alert_id"] for d in targets])
    stamp = now_iso()
    delivered: list[dict] = []
    with transaction() as conn:
        for d in targets:
            if d["alert_id"] in already:
                continue
            alert = query_one("SELECT * FROM alerts WHERE id=?", (d["alert_id"],))
            payload = {
                "sequence": d["sequence"],
                "device_id": alert["device_id"],
                "batch_id": alert["batch_id"],
                "level": alert["level"],
                "alert_type": alert["alert_type"],
                "message": alert["message"],
                "alert_time": alert["alert_time"],
                "verdict": "pass",
                "decision_reason": d["reason"],
                "forwarded_at": stamp,
                "raw": json.loads(alert["payload_json"] or "{}"),
            }
            cur = conn.execute(
                """INSERT INTO receiver_events(alert_id, sequence, payload_json,
                       received_at, source) VALUES (?,?,?,?, 'forward')""",
                (d["alert_id"], d["sequence"], json.dumps(payload, ensure_ascii=False), stamp),
            )
            delivered.append({"id": cur.lastrowid, **payload})
    return delivered


def forward_all_pending() -> list[dict]:
    """显式重放：把当前所有尚未投递的 PASS 告警投递到模拟接收端。"""
    return _record_forward(list_decisions())


def list_inbox(limit: int = 200) -> list[dict]:
    rows = query_all(
        "SELECT * FROM receiver_events ORDER BY id DESC LIMIT ?", (limit,)
    )
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item.pop("payload_json"))
        out.append(item)
    return out


def clear_inbox() -> None:
    execute("DELETE FROM receiver_events")


def receive_webhook(payload: Any) -> dict:
    """模拟接收端也支持直接 POST（便于用 curl 验证接收端存活）。"""
    stamp = now_iso()
    seq = payload.get("sequence") if isinstance(payload, dict) else None
    aid = payload.get("alert_id") if isinstance(payload, dict) else None
    cur = execute(
        """INSERT INTO receiver_events(alert_id, sequence, payload_json,
               received_at, source) VALUES (?,?,?,?, 'webhook')""",
        (aid, seq, json.dumps(payload, ensure_ascii=False), stamp),
    )
    return {"id": cur.lastrowid, "received_at": stamp, "source": "webhook",
            "echo": payload}


# ---------------------------------------------------------------- 注入 / 导入 / 撤销

def _next_sequence() -> int:
    row = query_one("SELECT COALESCE(MAX(sequence), 0) + 1 AS s FROM alerts")
    return int(row["s"])


def _insert_alert(normalized: dict, sequence: int) -> int:
    cur = execute(
        """INSERT INTO alerts(sequence, device_id, batch_id, level, alert_type,
               message, alert_time, payload_json) VALUES (?,?,?,?,?,?,?,?)""",
        (
            sequence, normalized["device_id"], normalized["batch_id"],
            normalized["level"], normalized["alert_type"], normalized["message"],
            normalized["alert_time"], normalized["payload_json"],
        ),
    )
    return int(cur.lastrowid)


def inject_alert(raw: dict, auto_forward: bool | None = None) -> dict:
    """逐条注入：归一化 → 入库 → 全量重评 → 若放行且开启自动转发则投递。"""
    normalized = normalize_alert(raw)
    sequence = _next_sequence()
    alert_id = _insert_alert(normalized, sequence)
    recompute()
    decision = next(d for d in list_decisions() if d["alert_id"] == alert_id)
    should_forward = (
        get_setting("auto_forward") == "1" if auto_forward is None else auto_forward
    )
    delivered = _record_forward([decision]) if should_forward else []
    return {
        "alert_id": alert_id,
        "sequence": sequence,
        "normalized": normalized,
        "decision": {k: decision[k] for k in
                     ("verdict", "rule_name", "reason", "cooldown_seconds", "explanation")},
        "forwarded": bool(delivered),
    }


def import_alerts(raws: list[dict], auto_forward: bool = False) -> dict:
    """批量导入：逐条归一化，遇到非法条目收集错误而不是整体失败。"""
    accepted: list[dict] = []
    errors: list[dict] = []
    sequence = _next_sequence()
    with transaction():
        for index, raw in enumerate(raws):
            try:
                normalized = normalize_alert(raw)
            except ValueError as exc:
                errors.append({"index": index, "error": str(exc)})
                continue
            alert_id = _insert_alert(normalized, sequence)
            accepted.append({"index": index, "alert_id": alert_id,
                             "sequence": sequence, **normalized})
            sequence += 1
    recompute()
    delivered = _record_forward(list_decisions()) if auto_forward else []
    return {
        "imported": len(accepted),
        "errors": errors,
        "forwarded": len(delivered),
        "accepted": accepted,
    }


def undo_last_injection() -> dict:
    """撤销最近一次注入（按最大 sequence），同时清掉它在接收端产生的转发记录。"""
    last = query_one("SELECT * FROM alerts ORDER BY sequence DESC LIMIT 1")
    if last is None:
        raise LookupError("没有可撤销的告警")
    with transaction() as conn:
        conn.execute("DELETE FROM receiver_events WHERE alert_id=?", (last["id"],))
        conn.execute("DELETE FROM alerts WHERE id=?", (last["id"],))
    recompute()
    return {"removed_alert_id": last["id"], "removed_sequence": last["sequence"],
            "device_id": last["device_id"], "level": last["level"]}


def clear_alerts() -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM receiver_events WHERE source='forward'")
        conn.execute("DELETE FROM alerts")
    recompute()


# ---------------------------------------------------------------- 规则 CRUD

RULE_FIELDS = ("name", "enabled", "priority", "action", "devices", "batches",
               "levels", "time_window", "cooldown_sec", "cooldown_scope", "note")


def create_rule(data: dict) -> dict:
    execute(
        """INSERT INTO rules(name, enabled, priority, action, devices, batches,
               levels, time_window, cooldown_sec, cooldown_scope, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data["name"], 1 if data["enabled"] else 0, data["priority"],
            data["action"], data["devices"], data["batches"], data["levels"],
            data["time_window"], data["cooldown_sec"], data["cooldown_scope"],
            data["note"], now_iso(),
        ),
    )
    recompute()
    return dict(query_one("SELECT * FROM rules WHERE id=last_insert_rowid()"))


def update_rule(rule_id: int, data: dict) -> dict:
    row = query_one("SELECT * FROM rules WHERE id=?", (rule_id,))
    if row is None:
        raise LookupError(f"规则 #{rule_id} 不存在")
    fields = {k: v for k, v in data.items() if k in RULE_FIELDS}
    if not fields:
        return dict(row)
    sets = ", ".join(f"{k}=?" for k in fields)
    params = [
        (1 if v else 0) if isinstance(v, bool) else v
        for v in fields.values()
    ]
    params.append(rule_id)
    execute(f"UPDATE rules SET {sets} WHERE id=?", tuple(params))
    recompute()
    return dict(query_one("SELECT * FROM rules WHERE id=?", (rule_id,)))


def delete_rule(rule_id: int) -> None:
    row = query_one("SELECT id FROM rules WHERE id=?", (rule_id,))
    if row is None:
        raise LookupError(f"规则 #{rule_id} 不存在")
    execute("DELETE FROM rules WHERE id=?", (rule_id,))
    recompute()


# ---------------------------------------------------------------- 设置

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        """INSERT INTO settings(key, value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )


# ---------------------------------------------------------------- 策略快照与对比

def list_versions() -> list[dict]:
    rows = query_all(
        "SELECT id, label, created_at, "
        "json_array_length(rules_json) AS rule_count FROM policy_versions "
        "ORDER BY id DESC"
    )
    return [dict(r) for r in rows]


def create_version(label: str) -> dict:
    rules = list_rules()
    cur = execute(
        "INSERT INTO policy_versions(label, rules_json, created_at) VALUES (?,?,?)",
        (label.strip(), json.dumps(rules, ensure_ascii=False), now_iso()),
    )
    return {"id": cur.lastrowid, "label": label.strip(), "rule_count": len(rules)}


def delete_version(version_id: int) -> None:
    row = query_one("SELECT id FROM policy_versions WHERE id=?", (version_id,))
    if row is None:
        raise LookupError(f"快照 #{version_id} 不存在")
    execute("DELETE FROM policy_versions WHERE id=?", (version_id,))


def _load_version_rules(version_id: int) -> list[dict]:
    row = query_one("SELECT * FROM policy_versions WHERE id=?", (version_id,))
    if row is None:
        raise LookupError(f"快照 #{version_id} 不存在")
    return json.loads(row["rules_json"])


def compare_with_version(base_version_id: int | None = None) -> dict:
    """用同一份告警分别跑“基线快照规则”和“当前规则”，逐条对比决定差异。"""
    versions = list_versions()
    if not versions:
        raise LookupError("还没有策略快照，请先在规则页保存一个快照")
    if base_version_id is None:
        base_version_id = versions[-1]["id"]  # 最早的快照
    base_row = query_one("SELECT * FROM policy_versions WHERE id=?", (base_version_id,))
    if base_row is None:
        raise LookupError(f"快照 #{base_version_id} 不存在")

    alerts = _alerts_for_engine()
    base_decisions = {
        d["alert_id"]: d
        for d in engine.evaluate(json.loads(base_row["rules_json"]), alerts)
    }
    current_rules = _rules_for_engine()
    current_decisions = {
        d["alert_id"]: d for d in engine.evaluate(current_rules, alerts)
    }

    rows = []
    changed = 0
    for alert in alerts:
        b, c = base_decisions[alert["id"]], current_decisions[alert["id"]]
        diff = b["verdict"] != c["verdict"] or b["rule_id"] != c["rule_id"] \
            or b["reason"] != c["reason"]
        changed += 1 if diff else 0
        rows.append({
            "alert_id": alert["id"],
            "sequence": alert["sequence"],
            "device_id": alert["device_id"],
            "batch_id": alert["batch_id"],
            "level": alert["level"],
            "alert_type": alert["alert_type"],
            "changed": diff,
            "before": {"verdict": b["verdict"], "rule_id": b["rule_id"],
                       "rule_name": b["rule_name"], "reason": b["reason"],
                       "cooldown_seconds": b["cooldown_seconds"],
                       "explanation": b["explanation"]},
            "after": {"verdict": c["verdict"], "rule_id": c["rule_id"],
                      "rule_name": c["rule_name"], "reason": c["reason"],
                      "cooldown_seconds": c["cooldown_seconds"],
                      "explanation": c["explanation"]},
        })

    base_pass = sum(1 for d in base_decisions.values() if d["verdict"] == "pass")
    cur_pass = sum(1 for d in current_decisions.values() if d["verdict"] == "pass")
    return {
        "base_version": {"id": base_row["id"], "label": base_row["label"],
                         "created_at": base_row["created_at"]},
        "summary": {
            "alerts": len(alerts),
            "before_pass": base_pass,
            "before_suppress": len(alerts) - base_pass,
            "after_pass": cur_pass,
            "after_suppress": len(alerts) - cur_pass,
            "changed": changed,
        },
        "rows": rows,
    }


# ---------------------------------------------------------------- 演示数据

def _today_at(hour: int, minute: int = 0, second: int = 0) -> str:
    now = datetime.now().astimezone()
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0).isoformat(timespec="seconds")


def seed_demo() -> dict:
    """灌入一套冷链场景演示数据（会清空现有告警/规则/快照/收件箱）。"""
    with transaction() as conn:
        for table in ("receiver_events", "decisions", "alerts", "rules",
                      "policy_versions"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('alerts','rules','receiver_events','policy_versions')")

    demo_rules = [
        dict(name="冷库门禁维护白名单", enabled=True, priority=10, action="pass",
             devices="DOOR-COLD-01", batches="", levels="",
             time_window="22:00-06:00", cooldown_sec=0,
             cooldown_scope="device_batch",
             note="夜间巡检开门属计划内作业，任何级别都放行"),
        dict(name="维护期批次静默", enabled=True, priority=20, action="suppress",
             devices="", batches="B-2026-0901", levels="",
             time_window="08:00-12:00", cooldown_sec=0,
             cooldown_scope="device_batch",
             note="批次 B-2026-0901 上午维护窗口，全部静默（但白名单规则优先级更高）"),
        dict(name="温度抖动去重", enabled=True, priority=30, action="suppress",
             devices="", batches="", levels="warning",
             time_window="", cooldown_sec=600, cooldown_scope="device_batch",
             note="同设备同批次 warning 10 分钟内只通知第一次"),
        dict(name="冷藏车运输提示降噪", enabled=True, priority=40, action="suppress",
             devices="REEFER-*", batches="", levels="info",
             time_window="", cooldown_sec=300, cooldown_scope="device",
             note="冷藏车 info 类消息同车 5 分钟冷却"),
    ]
    for r in demo_rules:
        execute(
            """INSERT INTO rules(name, enabled, priority, action, devices, batches,
                   levels, time_window, cooldown_sec, cooldown_scope, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["name"], 1, r["priority"], r["action"], r["devices"], r["batches"],
             r["levels"], r["time_window"], r["cooldown_sec"], r["cooldown_scope"],
             r["note"], now_iso()),
        )

    demo_alerts = [
        dict(device_id="FREEZER-01", batch_id="B-2026-0901", level="critical",
             alert_type="TEMP_HIGH", message="库温 -12.1℃ 高于阈值 -18℃",
             alert_time=_today_at(9, 5),
             payload={"source": "demo"}),
        dict(device_id="FREEZER-01", batch_id="B-2026-0901", level="warning",
             alert_type="TEMP_DRIFT", message="库温缓慢回升 -16.2℃",
             alert_time=_today_at(9, 18), payload={"source": "demo"}),
        dict(device_id="DOOR-COLD-01", batch_id="B-2026-0901", level="warning",
             alert_type="DOOR_OPEN", message="冷库门被打开（巡检）",
             alert_time=_today_at(22, 30), payload={"source": "demo"}),
        dict(device_id="FREEZER-02", batch_id="B-2026-0907", level="warning",
             alert_type="TEMP_DRIFT", message="风机异常导致温度漂移",
             alert_time=_today_at(14, 0), payload={"source": "demo"}),
        dict(device_id="FREEZER-02", batch_id="B-2026-0907", level="warning",
             alert_type="TEMP_DRIFT", message="温度漂移持续（3 分钟后）",
             alert_time=_today_at(14, 3), payload={"source": "demo"}),
        dict(device_id="FREEZER-02", batch_id="B-2026-0907", level="warning",
             alert_type="TEMP_DRIFT", message="温度漂移持续（15 分钟后）",
             alert_time=_today_at(14, 15), payload={"source": "demo"}),
        dict(device_id="REEFER-77", batch_id="B-2026-0910", level="info",
             alert_type="GPS_HEARTBEAT", message="运输心跳正常",
             alert_time=_today_at(10, 0), payload={"source": "demo"}),
        dict(device_id="REEFER-77", batch_id="B-2026-0910", level="info",
             alert_type="GPS_HEARTBEAT", message="运输心跳正常（2 分钟后）",
             alert_time=_today_at(10, 2), payload={"source": "demo"}),
        dict(device_id="SENSOR-09", batch_id="B-2026-0911", level="critical",
             alert_type="SENSOR_OFFLINE", message="温度传感器离线",
             alert_time=_today_at(16, 40), payload={"source": "demo"}),
    ]
    seq = 1
    for raw in demo_alerts:
        normalized = normalize_alert(raw)
        _insert_alert(normalized, seq)
        seq += 1

    create_version("初始策略（沙盒演示）")
    recompute()
    delivered = _record_forward(list_decisions())
    return {"rules": len(demo_rules), "alerts": len(demo_alerts),
            "forwarded_to_receiver": len(delivered)}
