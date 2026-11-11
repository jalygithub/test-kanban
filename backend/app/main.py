"""FastAPI 入口。

启动顺序很重要：必须先 import netfix（经由 fetchers.client）安装网络修复，
再创建 FastAPI 实例，确保任何请求发出前 IPv4 优先与限速已生效。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import router  # 触发 netfix.install()
from app.api.calendar import router as calendar_router
from app import scheduler as post_close  # 必须在 netfix 安装之后（它 import akshare）
from app import feishu  # 飞书长连接（同上：import notify → fetchers）
from app.store import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    post_close.start()  # 每交易日 16:30 自动跑盘后流水线
    feishu.start()      # 飞书入站指令（没配凭据/没开就是空操作）
    logger.info("后端启动完成 v%s db=%s", __version__, db.settings.db_path)
    yield
    feishu.stop()
    post_close.stop()


app = FastAPI(
    title="股票市场可视化看板",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(calendar_router)


@app.get("/")
def root():
    return {"service": "stock-dashboard", "version": __version__,
            "docs": "/docs"}
