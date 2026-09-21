"""全部 HTTP API 路由：告警、规则、评估、对比、接收端、设置、演示。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import services
from ..schemas import CompareIn, ImportIn, InjectIn, RuleIn, VersionCreateIn

api = APIRouter(prefix="/api")


def _err(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ---------------------------------------------------------------- 概览 / 健康

@api.get("/health")
def health() -> dict:
    return {"status": "ok"}


@api.get("/overview")
def overview() -> dict:
    return services.overview()


# ---------------------------------------------------------------- 告警

@api.get("/alerts")
def get_alerts() -> dict:
    return {"alerts": services.list_alerts(), "decisions": services.list_decisions()}


@api.post("/alerts/inject")
def inject(body: InjectIn) -> dict:
    try:
        return services.inject_alert(body.alert, body.auto_forward)
    except ValueError as exc:
        raise _err(exc)


@api.post("/alerts/import")
def import_alerts(body: ImportIn) -> dict:
    if not body.alerts:
        raise HTTPException(status_code=400, detail="alerts 列表不能为空")
    return services.import_alerts(body.alerts, body.auto_forward)


@api.post("/alerts/undo")
def undo_last() -> dict:
    try:
        return services.undo_last_injection()
    except LookupError as exc:
        raise _err(exc, status=404)


@api.post("/alerts/clear")
def clear_alerts() -> dict:
    services.clear_alerts()
    return {"ok": True}


# ---------------------------------------------------------------- 规则

@api.get("/rules")
def get_rules() -> dict:
    return {"rules": services.list_rules()}


@api.post("/rules")
def create_rule(body: RuleIn) -> dict:
    return services.create_rule(body.model_dump())


@api.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleIn) -> dict:
    try:
        return services.update_rule(rule_id, body.model_dump())
    except LookupError as exc:
        raise _err(exc, status=404)


@api.patch("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int) -> dict:
    rules = {r["id"]: r for r in services.list_rules()}
    if rule_id not in rules:
        raise HTTPException(status_code=404, detail=f"规则 #{rule_id} 不存在")
    return services.update_rule(
        rule_id, {"enabled": not bool(rules[rule_id]["enabled"])}
    )


@api.delete("/rules/{rule_id}")
def remove_rule(rule_id: int) -> dict:
    try:
        services.delete_rule(rule_id)
    except LookupError as exc:
        raise _err(exc, status=404)
    return {"ok": True}


@api.post("/evaluate")
def evaluate_now() -> dict:
    services.recompute()
    return {"ok": True, "overview": services.overview(),
            "decisions": services.list_decisions()}


# ---------------------------------------------------------------- 设置

@api.get("/settings")
def get_settings() -> dict:
    return {"auto_forward": services.get_setting("auto_forward") == "1"}


@api.post("/settings/auto-forward")
def set_auto_forward(enabled: bool) -> dict:
    services.set_setting("auto_forward", "1" if enabled else "0")
    return {"auto_forward": enabled}


# ---------------------------------------------------------------- 策略快照与对比

@api.get("/versions")
def get_versions() -> dict:
    return {"versions": services.list_versions()}


@api.post("/versions")
def create_version(body: VersionCreateIn) -> dict:
    return services.create_version(body.label)


@api.delete("/versions/{version_id}")
def remove_version(version_id: int) -> dict:
    try:
        services.delete_version(version_id)
    except LookupError as exc:
        raise _err(exc, status=404)
    return {"ok": True}


@api.post("/compare")
def compare(body: CompareIn) -> dict:
    try:
        return services.compare_with_version(body.base_version_id)
    except LookupError as exc:
        raise _err(exc, status=404)


# ---------------------------------------------------------------- 模拟接收端

@api.get("/receiver/inbox")
def inbox() -> dict:
    return {"events": services.list_inbox()}


@api.post("/receiver/forward-pending")
def forward_pending() -> dict:
    delivered = services.forward_all_pending()
    return {"delivered": len(delivered), "events": delivered}


@api.post("/receiver/clear")
def receiver_clear() -> dict:
    services.clear_inbox()
    return {"ok": True}


# ---------------------------------------------------------------- 演示数据

@api.post("/demo/load")
def load_demo() -> dict:
    return services.seed_demo()
