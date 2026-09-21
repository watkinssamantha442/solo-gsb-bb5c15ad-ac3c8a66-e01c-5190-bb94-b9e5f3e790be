"""FastAPI 应用入口：API 路由 + 模拟接收端 webhook + 静态前端。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import services
from .db import get_conn
from .routers.api import api

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="冷链告警抑制策略沙盒",
    description="在本地沙盒里编写抑制规则、逐条注入告警、对比策略变更前后结果。",
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    # 首次启动时初始化数据库
    get_conn()


@app.get("/webhook", include_in_schema=False)
def webhook_info() -> dict:
    """探测地址：提示用 POST 投递。"""
    return {"name": "内置模拟接收端", "usage": "POST /webhook 投递任意 JSON"}


@app.post("/webhook", tags=["receiver"])
async def receive_webhook(request: Request) -> dict:
    """外部告警平台 webhook 的模拟落点，收到的内容进入“接收端”标签页。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw_text": (await request.body()).decode("utf-8", "replace")}
    return services.receive_webhook(payload)


app.include_router(api)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
