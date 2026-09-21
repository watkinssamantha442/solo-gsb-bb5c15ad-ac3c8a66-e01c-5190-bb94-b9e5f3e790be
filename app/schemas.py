"""Pydantic 请求模型（仅用于规则等强结构输入；告警导入接受宽松 JSON）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .normalize import canonical_level, normalize_levels_csv, validate_time_window  # noqa: F401

VALID_LEVELS = {"info", "warning", "critical"}
VALID_SCOPES = {"device_batch", "device", "global"}


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max=100)
    enabled: bool = True
    priority: int = Field(ge=1, le=10000)
    action: Literal["suppress", "pass"]
    devices: str = ""
    batches: str = ""
    levels: str = ""
    time_window: str = ""
    cooldown_sec: int = Field(default=0, ge=0, le=365 * 24 * 3600)
    cooldown_scope: Literal["device_batch", "device", "global"] = "device_batch"
    note: str = ""

    @field_validator("devices", "batches")
    @classmethod
    def _strip_csv(cls, v: str) -> str:
        return ",".join(p.strip() for p in str(v).split(",") if p.strip())

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: str) -> str:
        normalized = normalize_levels_csv(v)
        bad = [t for t in normalized.split(",") if t and t not in VALID_LEVELS]
        if bad:
            raise ValueError(f"级别仅支持 info/warning/critical，收到：{','.join(bad)}")
        return normalized

    @field_validator("time_window")
    @classmethod
    def _window(cls, v: str) -> str:
        try:
            return validate_time_window(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("name", "note")
    @classmethod
    def _text(cls, v: str) -> str:
        return v.strip()


class InjectIn(BaseModel):
    alert: dict
    auto_forward: bool | None = None  # None 时使用全局设置


class ImportIn(BaseModel):
    alerts: list[dict]
    auto_forward: bool = False  # 批量导入默认不转发，避免灌入收件箱


class VersionCreateIn(BaseModel):
    label: str = Field(min_length=1, max=80)


class CompareIn(BaseModel):
    base_version_id: int | None = None  # None 时使用最早快照
