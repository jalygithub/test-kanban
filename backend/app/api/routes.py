"""HTTP 路由。

M1：涨跌停池、板块资金流、任务状态
M3：连板梯队（并入 /limit 的 zt 行：连板数/涨停统计）
M4：主力建仓打分（/main-force，计算量大所以做成后台线程 + 状态轮询）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app import __version__
from app import cmdcfg, feishu, invite, notify, pay, scheduler, services, subscribe
from app.config import settings
from app.fetchers import calendar as tcal
from app.fetchers import fund_estimate as fund_fetch
from app.fetchers import hk as hk_fetch
from app.fetchers import live as live_fetch
from app.fetchers import news as news_fetcher
from app.fetchers import quotes
from app.fetchers import search as search_fetch
from app.fetchers.client import market_of, norm_code
from app.metrics import board_streak, mainforce, track
from app.metrics import fund_review as freview
from app.pipeline import backfill
from app.pipeline import coverage as pipeline_coverage
from app.pipeline import ingest as jobstate
from app.pipeline import replenish
from app.pipeline import tasks as tasksvc
from app.pipeline import top5track
from app.research import engine
from app.research.audit import BacktestDataAuditService
from app.store import db, logs
from app.trading import service as tradingsvc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "version": __version__}


def _exit_soon() -> None:
    """响应回到页面**之后**再动手，顺序不能反。

    前端 dev server 是页面与后端之间的唯一通路：端点里同步杀掉它，按钮就
    永远等不到 200（代理只会回一个 Bad Gateway），用户看到的是报错而不是
    「服务已关闭」。BackgroundTasks 在响应体发完后才执行，所以这里先多等
    1 秒让结果送达，再杀前端、再退出本进程。
    """
    time.sleep(1.0)
    detail = services.stop_frontend()
    logger.warning("停止服务：前端 %s", detail.get("frontend"))
    time.sleep(0.3)
    logger.warning("后端进程退出（来自停止服务请求）")
    # os._exit 跳过 lifespan 清理：调度器线程是 daemon，跟着走就行
    os._exit(0)


@router.post("/system/stop")
def system_stop(background: BackgroundTasks) -> Dict[str, Any]:
    """停止整个看板：先把结果回给按钮，随后（后台任务）结束前端与本进程。

    页面左侧菜单最下方的「停止服务」按钮走这里。盘后任务正在跑时拒绝（409），
    免得打到一半把 12-15 分钟的打分作废；想再开就双击桌面「A股看板一键启动」。
    """
    if scheduler.state()["running"]:
        raise HTTPException(409, "盘后任务正在跑，等它结束再停止服务")

    background.add_task(_exit_soon)
    return {
        "ok": True,
        "note": "正在关闭前后端（约 1.5 秒），需要时双击桌面「A股看板一键启动」重新开启",
    }


class PayBaseBody(BaseModel):
    """链接前缀。收银台与邀请链接共用一个（`pay.base_url()`）。"""
    url: str = Field(..., min_length=8, max_length=200,
                     description="形如 http://192.168.137.10:5173")


@router.get("/system/pay-base-url")
def pay_base_url() -> Dict[str, Any]:
    """链接前缀：手机热点 / 内网 / 以后的公网域名都在这里换。

    **不用改 `.env` 也不用重启**（存 `kv`，即时生效）—— 热点的网段 IP 每次开
    都可能变，改 `.env` 再重启 8000 太重了。
    """
    return {"url": pay.base_url(), "default": str(settings.pay_base_url or "")}


@router.post("/system/pay-base-url")
def pay_base_url_set(body: PayBaseBody) -> Dict[str, Any]:
    """改链接前缀。**只影响之后新生成的链接**，已发出去的是快照、不动。"""
    try:
        url = pay.set_base_url(body.url)
    except pay.PayError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": url}


@router.get("/jobs/status")
def job_status() -> Dict[str, Any]:
    """任务状态：上次跑的结果 + 当前进度 + 下一次定时触发。"""
    return {
        "last": db.last_job(),
        "current": scheduler.state(),
        "schedule": scheduler.schedule_info(),
    }


@router.post("/jobs/run")
def jobs_run(
    date: Optional[str] = Query(None, description="YYYYMMDD，缺省取目标交易日"),
    force: bool = Query(False, description="true=当天已打分也强制重算"),
) -> Dict[str, Any]:
    """后台跑一轮**完整**盘后流水线（含打分，约 12-15 分钟），立即返回。

    调度器只在下一个交易日的 16:30 触发，启动时不会自动补跑今天的任务
    （那会未经告知就跑十几分钟）。需要现在补跑就调这里，或用总览页的按钮。
    """
    if scheduler.state()["running"]:
        raise HTTPException(409, "已有一轮盘后任务在进行中")

    def _worker() -> None:
        try:
            scheduler.run_post_close(date_str=date, force=force, kind="manual")
        except Exception:  # noqa: BLE001
            logger.exception("手动盘后任务失败")

    # 状态必须在**起线程之前**同步置位，否则紧接的第一次 status 轮询还是 false
    threading.Thread(target=_worker, name="post-close-manual",
                     daemon=True).start()
    return {"status": "started", "date": date, "state": scheduler.state()}


class ScheduleBody(BaseModel):
    """两个开关**各自独立**（2026-10-12 用户拍板）：14:50 推送只是发一条消息，
    16:30 盘后打分要跑十几分钟，绑在一起就变成「想收推送就得把打分也跑上」。
    两个字段都留空 = 什么都不改，只回最新状态。
    """
    enabled: Optional[bool] = Field(None, description="盘后定时任务（16:30 抓取+打分）")
    push_enabled: Optional[bool] = Field(None, description="14:50 估值推送")


@router.post("/jobs/schedule")
def jobs_schedule(body: ScheduleBody) -> Dict[str, Any]:
    """开/关定时调度。选择写进 SQLite，重启后仍然生效。

    只传 `enabled` 的老调用方不受影响 —— 另一个字段留空就是不去动它。
    """
    if body.enabled is not None:
        scheduler.set_enabled(body.enabled)
    if body.push_enabled is not None:
        scheduler.set_push_enabled(body.push_enabled)
    return {"schedule": scheduler.schedule_info()}


@router.get("/jobs/history")
def jobs_history(limit: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """最近若干次任务记录（新的在前），给「定时任务」页看运行历史。"""
    return {"items": db.jobs(limit)}


class NotifyBody(BaseModel):
    """飞书推送配置。webhook 与密钥存 SQLite，不进代码仓库。"""
    enabled: bool = Field(False, description="true=定时任务跑完后推送飞书")
    webhook: str = Field("", description="飞书自定义机器人 webhook 地址")
    secret: str = Field("", description="签名校验密钥，机器人没开安全设置就留空")
    sections: Optional[List[str]] = Field(
        None, description="要推送的分段 key 列表；null=保持现有勾选，[]=一个都不推")


@router.get("/jobs/notify")
def jobs_notify() -> Dict[str, Any]:
    """推送配置 + 分段清单 + 上次发送结果（「定时任务」页的「跑完通知」卡片）。

    ``section_defs`` 由后端下发，前端照着渲染勾选框 —— 加一段正文只改
    ``notify.SECTION_DEFS`` 一处，页面自动多一项，两边不会各改一半。
    """
    return {"notify": notify.config()}


@router.post("/jobs/notify")
def jobs_notify_set(body: NotifyBody) -> Dict[str, Any]:
    """保存推送配置。改了立即生效，不用重启后端。

    只有**定时触发**的盘后任务跑完才推送；手动「立即执行」与盘中轻量抓取
    都不发。手动跑完想收一条就点「推送摘要」（`POST /jobs/notify/digest`）。

    ``sections`` 省略（None）时保持现有勾选，所以只改地址的老调用方不会
    意外把勾选清掉。
    """
    try:
        return {"notify": notify.set_config(body.enabled, body.webhook,
                                            body.secret, body.sections)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/jobs/notify/test")
def jobs_notify_test() -> Dict[str, Any]:
    """按库内最新数据拼一条摘要并**立即发送**，正文一并回给前端做预览。

    发送结果（飞书返回的 code/关键词/签名校验错误）原样透出，便于排查。
    """
    date = _latest_trade_date()
    if not date:
        raise HTTPException(404, "库内无数据，先跑一轮任务再测试")
    try:
        return notify.preview(date)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"测试发送失败: {type(exc).__name__}: {exc}") from exc


def _digest_args() -> Dict[str, Any]:
    """把最近一轮任务记录翻成 `notify.after_job` 要的参数。

    `job_run.detail` 正常是一段 JSON（`_finish` 写的，含 `date` / `failed`）；
    只有 `_collect` 中途抛异常那条路径会把 `类型: 异常` 原样写进去 —— 那种情况
    拿不到日期，回退到库内最近交易日、失败步骤留空（status 本身已经是
    `failed`，摘要里照样看得出这轮挂了，不用编造一个失败清单）。

    没有任务记录直接 404：库里可能只有演示数据，硬凑一条「定时任务 · 成功」
    是在骗人；正在跑的那条（status=running）也挡回去 —— 状态还没落定，
    摘要要么缺最后几步，要么用时是错的。
    """
    job = db.last_job()
    if not job:
        raise HTTPException(404, "没有任务记录，先跑一轮任务再推送")
    status = str(job.get("status") or "")
    if status == "running":
        raise HTTPException(409, "有一轮任务正在进行中，跑完再推")

    date, failed = "", []
    try:
        detail = json.loads(str(job.get("detail") or ""))
        if isinstance(detail, dict):
            date = str(detail.get("date") or "")
            failed = [str(x) for x in (detail.get("failed") or [])]
    except (TypeError, ValueError):
        pass

    # 用时从 job_start / job_finish 落库的两个 UTC 时间戳算，不自己计时：
    # 这里是「事后取数」，没有起点，编一个出来比不显示更糟
    elapsed: Optional[float] = None
    try:
        started = datetime.fromisoformat(str(job.get("started_at") or ""))
        finished = datetime.fromisoformat(str(job.get("finished_at") or ""))
        elapsed = max((finished - started).total_seconds(), 0.0) or None
    except (TypeError, ValueError):
        pass

    return {"date": date, "status": status, "failed": failed,
            "kind": str(job.get("kind") or "manual"), "elapsed": elapsed}


@router.post("/jobs/notify/digest")
def jobs_notify_digest() -> Dict[str, Any]:
    """把最近一轮盘后任务的**真实结果**拼成摘要发出去，正文与自动推送一致。

    与「测试发送」的区别：那条恒为「测试发送 · 测试成功」，只证明通路是通的；
    这条的状态、触发方式（定时 / 手动 / 飞书指令）、失败步骤全取自 `job_run`，
    与自动推送同源，还会顺手补关注股的逐日资金流。

    手动「立即执行」跑完不会自动发（见 `scheduler.run_post_close`），
    想发就点这里。受「跑完通知」开关约束的是**自动**推送，手动按钮传
    `require_enabled=False` —— 人已经在页面上点了发送，开关管不到这件事。
    配置缺失时返回 `skipped`，前端如实提示原因而不是假装发成功。
    """
    args = _digest_args()
    date = args["date"] or _latest_trade_date()
    if not date:
        raise HTTPException(404, "库内无数据，先跑一轮任务再推送")
    try:
        return notify.after_job(date, status=args["status"], failed=args["failed"],
                                elapsed=args["elapsed"], kind=args["kind"],
                                require_enabled=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"推送失败: {type(exc).__name__}: {exc}") from exc


class FeishuBody(BaseModel):
    """飞书入站指令的凭据与白名单。存 SQLite，不进代码仓库。"""
    app_id: str = Field("", description="企业自建应用 App ID（cli_ 开头）")
    app_secret: str = Field("", description="企业自建应用 App Secret")
    owner: str = Field("", description="只响应这个发送人（open_id），留空=暂不响应")
    enabled: bool = Field(False, description="true=开长连接收消息")
    reply_mode: Optional[str] = Field(
        None, pattern="^(private|group)$",
        description="群里指令回复方式：private=私发给发送人（默认）/ "
                    "group=回原会话；不传=不改（防旧页面覆盖）")


class FeishuSimBody(BaseModel):
    """模拟一条收到的文本指令，用来在不碰真实连接时验证解析与权限。"""
    text: str = Field(..., min_length=1, max_length=200, description="指令原文")


@router.get("/feishu/config")
def feishu_config() -> Dict[str, Any]:
    """入站指令配置 + 连接状态（「定时任务」页的「飞书指令」卡片）。"""
    return {"feishu": feishu.config()}


@router.post("/feishu/config")
def feishu_config_set(body: FeishuBody) -> Dict[str, Any]:
    """保存并立即生效：预检凭据 → 需要时重启长连接，不用重启后端。

    `reply_mode` 不传 = 不动该开关（旧页面只发凭据四件套时不能把它抹回默认）。
    """
    return {"feishu": feishu.set_config(
        body.app_id, body.app_secret, body.owner, body.enabled,
        reply_mode=body.reply_mode)}


@router.post("/feishu/simulate")
def feishu_simulate(body: FeishuSimBody) -> Dict[str, Any]:
    """把一条指令按分发逻辑走一遍并回回复正文，**不执行副作用**。

    跑任务、改关注这类指令只回「会做什么」，方便在页面上试而不惊动真实数据。
    """
    try:
        return feishu.simulate(body.text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("模拟飞书指令失败")
        raise HTTPException(500, f"模拟失败: {type(exc).__name__}: {exc}") from exc


@router.post("/feishu/push")
def feishu_push() -> Dict[str, Any]:
    """**主动**往飞书推一条看板状态：发给白名单那个人的单聊（owner 为空则发到
    机器人所在的第一个群）。用来随时验证「应用能不能自己发起一条消息」。
    """
    try:
        return {"push": feishu.push_status()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("飞书主动推送失败")
        raise HTTPException(502, f"推送失败: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------ 指令管理
#
# 页面 `pages/Cmds.tsx`。**一张表管两类**：内置指令的默认值在 `cmdcfg.BUILTIN`
# （可改触发词/别名/说明/附带文案/是否进快捷卡），自定义指令整条都在库里。
# 删内置那行 = 恢复默认，删自定义 = 彻底消失。
#
# 权限不动：内置的闸仍是 `feishu._ACCESS`，库里的 access 只做展示；
# 只有自定义的 access 生效，而且只有 sub / owner（`store/db.py` 卡的）。

class CmdItemBody(BaseModel):
    """一条指令的全部可改字段；`key` 决定是新建自定义还是改内置。"""
    key: str = Field(..., min_length=1, max_length=40, description="唯一标识")
    kind: str = Field("custom", description="builtin / custom")
    label: str = Field("", max_length=40, description="按钮上显示的名字")
    text: str = Field("", max_length=40, description="主触发文本（点按钮发的就是它）")
    alias: str = Field("", max_length=200, description="其余别名，逗号分隔")
    access: str = Field("owner", description="仅自定义生效：sub / owner")
    reply: str = Field("", max_length=500, description="执行后附带的推送文案，可空")
    target: str = Field("", max_length=80,
                        description="实际执行的指令原文；留空=只回 reply")
    enabled: bool = Field(True, description="false = 这条指令不再识别")
    quick: bool = Field(False, description="进快捷指令卡片")
    sort: int = Field(0, ge=0, le=9999, description="卡片里的排序，小的在前")


class CmdTplBody(BaseModel):
    """推送文案模板。**空串 = 恢复默认**。免责声明由代码追加，改不到它。"""
    kind: str = Field(..., min_length=1, description="fund_1450 / digest")
    text: str = Field("", max_length=200, description="摘要抬头可用 {date} 代入日期")


class CmdCardBody(BaseModel):
    """把快捷指令卡片发到飞书。不填 `to` 就发给白名单那个人的单聊。"""
    to: str = Field("", max_length=64, description="open_id 或 chat_id")
    id_type: str = Field("open_id", description="open_id / chat_id")


def _tpl_payload() -> Dict[str, Any]:
    return {"templates": {k: {"label": cmdcfg.TPL_LABEL[k],
                              "default": cmdcfg.tpl_default(k),
                              "text": cmdcfg.tpl_text(k)}
                          for k in cmdcfg.TPL_KINDS}}


@router.get("/cmds")
def cmds_list() -> Dict[str, Any]:
    """指令列表（内置 ∪ 改动 ∪ 自定义）+ 两个推送文案模板的当前值。"""
    return {"items": cmdcfg.rows(), **_tpl_payload()}


@router.post("/cmds")
def cmds_save(body: CmdItemBody) -> Dict[str, Any]:
    """新增 / 修改一条。**校验不过就 400**：key 格式、自定义触发词撞内置指令、
    access 档位这三件事都在 `cmdcfg.save` 里当场拦，不写脏数据。
    """
    try:
        return {"item": cmdcfg.save(body.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/cmds/{key}")
def cmds_remove(key: str) -> Dict[str, Any]:
    """删一条：内置 = 恢复代码默认，自定义 = 彻底消失。"""
    return {"deleted": cmdcfg.remove(key)}


@router.post("/cmds/template")
def cmds_template(body: CmdTplBody) -> Dict[str, Any]:
    """保存推送文案模板。清空输入框即恢复默认，不会留下第三种状态。"""
    try:
        cmdcfg.tpl_set(body.kind, body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _tpl_payload()


@router.get("/cmds/quick-card")
def cmds_quick_card_preview() -> Dict[str, Any]:
    """只**拼**卡片 JSON、不发送 —— 页面上先看长相，也是一条零网络的回归判据。"""
    return {"card": feishu.quick_card()}


@router.post("/cmds/quick-card")
def cmds_quick_card_send(body: CmdCardBody) -> Dict[str, Any]:
    """把快捷指令卡片发到飞书。**连不上就如实报错**，绝不回假成功。"""
    try:
        out = feishu.push_quick_card(body.to, body.id_type)
    except Exception as exc:  # noqa: BLE001
        logger.exception("发快捷指令卡片失败")
        raise HTTPException(502, f"发送失败: {type(exc).__name__}: {exc}") from exc
    if not out.get("ok"):
        raise HTTPException(502, str(out.get("reason") or "发送失败"))
    return out


# ------------------------------------------------------------ 订阅管理（docs/06）
#
# 页面 `pages/Subs.tsx`，**owner 专属**。这些接口只服务后台管理，订阅者本人
# 用的是飞书指令那条路（`feishu.dispatch` 里过 `access="sub"` 的闸）。

class SubActivateBody(BaseModel):
    """开通 / 续期。P0 走人工：占位支付链接没有回调，钱和开通各走各的。"""
    open_id: str = Field(..., min_length=1, max_length=64,
                         description="飞书 open_id")
    amount: Optional[float] = Field(None, gt=0, description="金额；留空用全局 pro_price")
    nickname: str = Field("", max_length=64, description="备注名（群昵称等）")


class SubUpdateBody(BaseModel):
    """行内编辑。`None` = 这一项不改。"""
    amount: Optional[float] = Field(None, gt=0)
    fund_limit: Optional[int] = Field(None, description="单独关注上限；0=恢复全局默认")
    nickname: Optional[str] = Field(None, max_length=64)
    close: bool = Field(False, description="true = 立即关闭订阅（关注不清空）")


class PricingBody(BaseModel):
    """收银台定价。**语义校验在 `pay.set_pricing`**（抛 `PayError` 转 400），
    这里只声明形状 —— 让「单价是 0」和「单价写成字符串」分属两种状态码没意义。"""
    price: float = Field(..., description="每月单价（元）")
    months: List[int] = Field(default_factory=list, description="可选档位（月数）")
    amounts: Dict[str, float] = Field(
        default_factory=dict,
        description="某档单独定价；缺哪档 = 那档按 单价 × 月数")


def _sub_row(row: Dict[str, Any],
             pushes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """列表的一行：订阅信息 + 关注计数 + 推送汇总拼在一起，前端不用再拉三个接口。"""
    oid = str(row.get("open_id") or "")
    push = pushes.get(oid) or {}
    out = dict(row)
    out.update({
        "active": subscribe.is_active(oid),
        "fund_count": len(db.sub_funds(oid)),
        "limit": subscribe.limit_of(oid),
        "push": {
            "total": push.get("total") or 0,
            "ok": push.get("ok") or 0,
            "fail": push.get("fail") or 0,
            "last_at": push.get("last_at") or "",
        },
    })
    return out


@router.get("/subs")
def subs_list() -> Dict[str, Any]:
    """订阅管理页的列表 + 当前生效的全局配置（页面上显示默认值用）。"""
    pushes = db.push_log_summary()
    return {
        "items": [_sub_row(r, pushes) for r in db.sub_list()],
        "settings": {
            "pro_price": settings.pro_price,
            "pro_days": settings.pro_days,
            "fund_watch_limit": settings.fund_watch_limit,
            "push_hour": settings.push_hour,
            "push_minute": settings.push_minute,
        },
    }


@router.post("/subs")
def subs_activate(body: SubActivateBody) -> Dict[str, Any]:
    """开通 / 续期。**续期从原到期日往后累加**，过期了才从现在起算。"""
    open_id = body.open_id.strip()
    try:
        row = db.sub_activate(open_id, amount=body.amount,
                              nickname=body.nickname)
    except Exception as exc:  # noqa: BLE001
        logger.exception("开通订阅失败")
        raise HTTPException(500, f"开通失败: {type(exc).__name__}: {exc}") from exc
    return {"item": _sub_row(row, db.push_log_summary())}


@router.post("/subs/push")
def subs_push_now() -> Dict[str, Any]:
    """立刻跑一次 14:50 推送 —— 给 `_warn_if_missed_push` 那句告警的手动入口。

    **不查交易日**：定时那道闸拦的是「到点自动发」，人主动点的是另一回事，
    要不要发由点的人决定。
    """
    try:
        return {"push": subscribe.run_push()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("手动推送失败")
        raise HTTPException(500, f"推送失败: {type(exc).__name__}: {exc}") from exc


# ---- 下面两条**必须排在 `/subs/{open_id}` 之前** ----
# FastAPI 按注册顺序匹配：`/subs/pricing` 与 `/subs/{open_id}` 都是两段路径，
# 注册晚了就会被 `{open_id}="pricing"` 抢走 —— 与下面 `/pay/orders` 是同一个坑，
# `_sub_check.py` 有对应回归。
@router.get("/subs/pricing")
def subs_pricing() -> Dict[str, Any]:
    """收银台定价（订阅管理页的定价卡片）。存 `kv` 表，改完即时生效、**不用重启**。"""
    return {"pricing": pay.pricing()}


@router.post("/subs/pricing")
def subs_pricing_save(body: PricingBody) -> Dict[str, Any]:
    """存定价并回读。金额校验在 `pay.set_pricing` 里。"""
    try:
        return {"pricing": pay.set_pricing(body.price, body.months, body.amounts)}
    except pay.PayError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/subs/{open_id}")
def subs_update(open_id: str, body: SubUpdateBody) -> Dict[str, Any]:
    """行内编辑：金额 / 单独关注上限 / 备注名 / 关闭订阅。"""
    open_id = open_id.strip()
    row = db.sub_get(open_id)
    if row is None:
        raise HTTPException(404, f"没有这个订阅者：{open_id}")
    if body.close:
        db.sub_close(open_id)
    if any(v is not None for v in (body.amount, body.fund_limit, body.nickname)):
        db.sub_set(open_id, amount=body.amount, fund_limit=body.fund_limit,
                   nickname=body.nickname)
    row = db.sub_get(open_id) or row
    return {"item": _sub_row(row, db.push_log_summary())}


@router.get("/subs/{open_id}/funds")
def subs_funds(open_id: str) -> Dict[str, Any]:
    """他关注了哪些基金（展开行看明细）。"""
    oid = open_id.strip()
    return {"items": db.sub_funds(oid), "limit": subscribe.limit_of(oid)}


@router.get("/subs/{open_id}/pushes")
def subs_pushes(open_id: str) -> Dict[str, Any]:
    """这个人的推送历史：发没发、成没成、失败原因。"""
    return {"items": db.push_log_tail(open_id.strip(), 50)}


# ------------------------------------------------------------ 收银台（docs/06 §2）
#
# **占位演示，收不到钱**：没有支付网关、没有回调、后端只监听本机/内网。
# `去支付` 生成一张待确认订单，管理员在 `/subs` 页照订单点确认开通。
#
# 前两个接口是**订阅者本人**用的（手机浏览器打开飞书里那条链接），
# 后两个是管理员在订阅管理页用的。三者的鉴权方式不同：
#   - /pay/{token}      —— token 即凭证（不可逆推 open_id，过期作废）
#   - /pay/orders*      —— 与其余后台接口一样靠 nginx basic_auth


class PayOrderBody(BaseModel):
    """下单。金额不在这里传 —— 后端按 `pro_price × months` 现算并快照，
    前端传金额只会造出「页面 297、库里 296.99」这种对不上的账。"""
    months: int = Field(..., ge=1, le=24, description="订阅月数")
    channel: str = Field(..., min_length=1, max_length=16,
                         description="wechat / alipay")
    note: str = Field("", max_length=200, description="留言（可选）")


@router.get("/pay/plans")
def pay_plans() -> Dict[str, Any]:
    """档位表与支付方式。收银台页面先拉这个，再让用户选。

    **不带 token** —— 档位是公开信息（页面还没确认 token 就要渲染选月卡片），
    拿它当探针会白送一个「token 对不对」的时序信号，没必要。
    """
    return {
        "plans": pay.plans(),
        "channels": [{"value": c, "label": pay.CHANNEL_LABELS[c]}
                     for c in pay.CHANNELS],
        "price": pay.unit_price(),
    }


def _pay_order_row(order: Dict[str, Any]) -> Dict[str, Any]:
    """订单行 + 渠道中文名。前端只认 `channel_label`，不在页面上做映射。"""
    out = dict(order)
    out["channel_label"] = pay.describe_channel(str(order.get("channel") or ""))
    return out


# ---- 下面两条**必须排在 `/pay/{token}` 之前** ----
# FastAPI 按注册顺序匹配：`/pay/orders` 与 `/pay/{token}` 都是两段路径，
# 注册晚了就会被 `{token}="orders"` 抢走，管理员的订单列表直接 404。
@router.get("/pay/orders")
def pay_orders() -> Dict[str, Any]:
    """待确认订单（订阅管理页的「收银台订单」卡片）。"""
    return {"items": [_pay_order_row(o) for o in pay.order_list()]}


@router.post("/pay/orders/{order_no}/confirm")
def pay_confirm(order_no: str) -> Dict[str, Any]:
    """管理员照订单开通。**重复确认被挡下**（返回 409）—— 否则连点两次就把
    订阅期间累加了两遍，是真会发生的操作事故。"""
    order = pay.confirm_order(order_no)
    if order is None:
        exist = db.pay_order_get(order_no)
        if exist is None:
            raise HTTPException(404, f"没有这张订单：{order_no}")
        raise HTTPException(409, f"订单 {order_no} 已经是「{exist['status']}」了，"
                                 f"不重复开通。")
    return {"order": _pay_order_row(order)}


@router.get("/pay/{token}")
def pay_info(token: str) -> Dict[str, Any]:
    """token → 这个人是谁 + 他的订阅状态 + 档位。收银台页面的首屏数据。

    **回给页面的只有昵称与到期时间，不回 open_id** —— 页面不需要它，
    少回一个身份标识就少一处泄露面（浏览器历史、截图、扩展都能拿到）。
    """
    who = pay.resolve(token)
    if not who:
        raise HTTPException(404, "收银台链接已失效，请在飞书重新发「订阅Pro」。")
    oid = str(who.get("open_id") or "")
    return {
        "nickname": who.get("nickname") or "",
        "plans": pay.plans(),
        "channels": [{"value": c, "label": pay.CHANNEL_LABELS[c]}
                     for c in pay.CHANNELS],
        "active": subscribe.is_active(oid),
        "expires_at": subscribe.expires_at(oid),
        "orders": [_pay_order_row(o) for o in pay.order_of_open_id(oid)],
    }


@router.post("/pay/{token}/order")
def pay_create_order(token: str, body: PayOrderBody) -> Dict[str, Any]:
    """下单。参数不对回 400（`PayError`），不刷 5xx 告警。

    金额不在请求里传 —— 后端按 `pro_price × months` 现算并快照，前端传金额
    只会造出「页面 297、库里 296.99」这种对不上的账。
    """
    try:
        order = pay.create_order(token, body.months, body.channel, body.note)
    except pay.PayError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"order": _pay_order_row(order)}


# ------------------------------------------------------------ 邀请链接
#
# 与收银台**并列**的一条链路（docs/06 §11）：owner 从名单里选人生成一条邀请
# 链接 → 对方点开看欢迎页 → 点「订阅」免费开通 30 天。
#
# ⚠ `/invite/list` **必须排在 `/invite/{token}` 之前** —— 两条都是 GET 两段路径，
# 注册晚了会被 `{token}="list"` 抢走。与 `/pay/orders` 是同一个坑，`_sub_check`
# 有对应回归。`/invite/join` 与 `/invite/make` 是 POST，跟 GET 的 `{token}` 不同
# 方法、不冲突，但也放在这里免得以后有人加个 GET `/invite/join` 踩进去。


class InviteMakeBody(BaseModel):
    """生成邀请链接。只传 open_id —— 天数走 `settings.pro_days`（30 天）。"""
    open_id: str = Field(..., min_length=1, description="要邀请的人")


class InviteJoinBody(BaseModel):
    """手工把人加进名单。**不调任何飞书接口** —— 见 `invite_join`。

    `pattern` 只挡「明显不是飞书 open_id」的输入（`ou_` 开头）—— 填错要到
    「发给他」才在飞书侧失败，那时链接已经生成了。测试与长连接落库的都是
    `ou_` 系，不会误伤。
    """
    open_id: str = Field(..., min_length=1, max_length=64,
                         pattern=r"^ou_[A-Za-z0-9_]+$",
                         description="要加进名单的人（飞书 open_id，ou_ 开头）")
    nickname: str = Field(default="", max_length=64, description="备注名，可空")


class InvitePushBody(BaseModel):
    """推送邀请链接：生成一条 + 私聊发给他，一次做完。"""
    open_id: str = Field(..., min_length=1, description="收件人")
    nickname: str = Field(default="", max_length=64, description="备注名，可空")


def _invite_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """名单行：补上 `active` / `expires_at` —— `sub_list` 只回原始字段。"""
    oid = str(r.get("open_id") or "")
    return {
        "open_id": oid,
        "nickname": r.get("nickname") or "",
        "plan": r.get("plan") or "",
        "active": subscribe.is_active(oid),
        "expires_at": subscribe.expires_at(oid),
        "amount": r.get("amount"),
    }


@router.get("/invite/list")
def invite_list() -> Dict[str, Any]:
    """邀请名单 = `subscriber` 表全部行（`plan=''` 是还没订阅的）。

    **不另开名单表**：发过消息的人由 `feishu._handle_event` 落进来，
    `sub_upsert` 本来就会给未订阅者建一行。
    """
    return {"items": [_invite_row(r) for r in invite.roster()],
            "pro_days": int(settings.pro_days)}


@router.post("/invite/join")
def invite_join(body: InviteJoinBody) -> Dict[str, Any]:
    """手工把一个人加进名单（owner 用）。

    正常入口是 `feishu._handle_event` 收到消息就自动落一行 —— 但**飞书长连接在
    公司内网连不上**，名单会一直是空的，这个接口就是给 owner 在那种环境下手工补
    人用的（**一行飞书接口都不调**，`open_id` 由你自己填）。

    威胁面与 `/subs` 的手工开通**完全一致**：后端本来就没有任何鉴权层
    （`docs/06 §9` 的 P1 待办），这里既不新开口子也不补权限。
    """
    return {"item": _invite_row(db.sub_upsert(body.open_id, body.nickname))}


@router.post("/invite/make")
def invite_make(body: InviteMakeBody) -> Dict[str, Any]:
    """给某个人生成一条邀请链接。**每次生成都是新的一条**，旧的到各自过期为止。"""
    try:
        link = invite.make_link(body.open_id)
    except invite.InviteError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"link": link}


@router.post("/invite/push")
def invite_push(body: InvitePushBody) -> Dict[str, Any]:
    """生成一条邀请链接并**用飞书私聊发给对方**（2026-10-14 用户定）。

    发送走 `feishu.send_text(open_id, text, id_type="open_id")` —— 现成能力，
    不新写飞书对接；也不用长连接（那是**收**消息的路，这里是**发**，互不相干）。

    **发私聊不发群 webhook**：链接带随机 token 且**一人一条**，进群就等于群里
    任何人点开都会把订阅开通到**他自己**头上（token 里烧的是生成时那个人的
    open_id）。私聊才对得上「这条是给你的」。

    `sent=false` 表示飞书拒了（凭据、应用权限、对方不可达）—— 链接**照样生成**，
    页面上还留着，复制手动发即可；不让「发不出去」把「拿到链接」也一起废掉。
    """
    try:
        link = invite.make_link(body.open_id, body.nickname)
    except invite.InviteError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Markdown 链接：`feishu.send_text` 认出 `[中文](url)` 会把整条升成富文本，
    # 飞书里显示成中文锚点；裸 URL 在文本消息里只能显示成 URL 本身。
    text = (
        f"【A股看板】你的专属开通链接（点开即开通 {int(settings.pro_days)} 天，免费）：\n"
        f"[点此打开邀请页]({link['url']})\n"
        f"一人一条，请勿转发；{int(settings.pro_days)} 天内有效。"
    )
    sent = feishu.send_text(body.open_id, text, id_type="open_id")
    return {"link": link, "sent": bool(sent)}


@router.get("/invite/{token}")
def invite_info(token: str) -> Dict[str, Any]:
    """欢迎页首屏：这条链接是给谁的 + 他现在订没订。

    **回昵称、不回 open_id** —— 与 `/pay/{token}` 同口径：页面要的只是显示个
    名字，少回一个身份标识就少一处泄露面（浏览器历史、截图、扩展都拿得到）。
    """
    who = invite.resolve(token)
    if not who:
        raise HTTPException(404, "邀请链接已失效，请向管理员重新索取。")
    oid = str(who.get("open_id") or "")
    return {
        "nickname": who.get("nickname") or "",
        "active": subscribe.is_active(oid),
        "expires_at": subscribe.expires_at(oid),
        "pro_days": int(settings.pro_days),
    }


@router.post("/invite/{token}/activate")
def invite_activate(token: str) -> Dict[str, Any]:
    """点「订阅」→ 免费开通。**已经在订阅期就只回状态、不重复累加天数**
    （否则把链接转给别人点两下，30 天就翻倍了）。"""
    try:
        out = invite.activate(token)
    except invite.InviteError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"result": out}


@router.get("/overview")
def overview() -> Dict[str, Any]:
    """总览页一次性聚合：交易日、各池家数、连板梯队、板块资金流、打分、任务。

    拆成八个小接口会让首屏变成八次往返，而这些本来就是同一天的同一份快照。
    全部只读本地 SQLite，不产生网络请求。
    """
    trade_date = _latest_trade_date()
    out: Dict[str, Any] = {"trade_date": trade_date}

    # ---- 股池家数 + 连板梯队 ------------------------------------
    pools: Dict[str, int] = {}
    ladder: Dict[str, int] = {}
    leader: Optional[Dict[str, Any]] = None
    # zt 那份**只 load 一次**：下面算梯队要逐行读，家数只要长度，原来两处各
    # load 一份同一天的 JSON（单天数百 KB），是白开的第二次。
    zt_rows: List[Dict[str, Any]] = []
    for kind in ("zt", "zb", "dt", "strong"):
        rows = db.load(f"pool_{kind}", trade_date) if trade_date else None
        pools[kind] = len(rows or [])
        if kind == "zt":
            zt_rows = rows or []
    for r in zt_rows:
        if not r.get("code"):
            continue
        try:
            boards = int(r.get("boards") or 1)
        except (TypeError, ValueError):
            boards = 1
        ladder[str(boards)] = ladder.get(str(boards), 0) + 1
        if leader is None or boards > int(leader.get("boards") or 0):
            leader = {"code": r.get("code"), "name": r.get("name"),
                      "boards": boards, "zt_stat": r.get("zt_stat"),
                      "industry": r.get("industry")}
    out["pools"] = pools
    out["ladder"] = ladder          # {"1": 40, "2": 8, ...} 由前端只展示 >=2
    out["leader"] = leader

    # ---- 板块资金流（净流入前 5 / 后 5）--------------------------
    sf = [r for r in (db.load("sector_flow_industry", "latest") or [])
          if isinstance(r.get("net_inflow"), (int, float))]
    sf.sort(key=lambda r: r["net_inflow"], reverse=True)
    out["sector_flow"] = {"top": sf[:5], "bottom": sf[-5:][::-1] if sf else []}

    # ---- 主力建仓打分（可能落后于 trade_date，各自取各自最新）----
    mf_date = db.latest_date("main_force")
    mf = db.load_one("main_force", mf_date) if mf_date else None
    out["main_force"] = {
        "date": mf_date,
        "count": len(mf.get("rows") or []) if mf else 0,
        "degraded": (mf.get("model") or {}).get("degraded", []) if mf else [],
        "top": (mf.get("rows") or [])[:5] if mf else [],
    }

    # ---- 关注 + 任务 + 数据新鲜度 -------------------------------
    out["watchlist_count"] = len(db.watch_list())
    out["job"] = {"last": db.last_job(), "current": scheduler.state(),
                  "schedule": scheduler.schedule_info()}
    out["freshness"] = [
        {"dataset": d, **(db.snapshot_meta(d) or {})}
        for d in ("pool_zt", "pool_zb", "pool_dt", "pool_strong",
                  "sector_flow_industry", "sector_flow_concept", "main_force")
    ]
    return out


@router.get("/dates")
def dates(dataset: str = Query("zt")) -> Dict[str, Any]:
    return {"dataset": dataset, "dates": db.list_dates(dataset)}


@router.get("/limit")
def limit_pool(
    date: Optional[str] = Query(None, description="YYYYMMDD，缺省取库内最新"),
    type: str = Query("zt", pattern="^(zt|zb|dt|strong)$"),
    refresh: bool = Query(False, description="true=强制重新抓取"),
) -> Dict[str, Any]:
    """涨停(zt) / 炸板(zb) / 跌停(dt) / 强势(strong) 股池。

    涨停池额外把**断板**的几只接在尾部（`broken=true`）—— 连板断了就不在当日
    池子里，不回看历史等于凭空消失。口径见 `metrics.board_streak`。
    """
    if refresh or date is None:
        if date is None:
            date = _latest_trade_date()
        if date is None:
            raise HTTPException(404, "库内无数据，请先触发 /api/refresh")

    if refresh:
        try:
            scheduler.fetch_pool(type, date)
        except db.DomainBusy as exc:
            # 有人正在抓同一天同一池（比如定时任务）—— 不是失败，409 让前端
            # 提示「稍后再试」。当成 502 报出去会让人以为源挂了。
            raise HTTPException(409, str(exc)) from exc
        return {"date": date, "type": type,
                "rows": _with_broken(db.load(f"pool_{type}", date), type, date)}

    rows = db.load(f"pool_{type}", date)
    if rows is None:
        try:
            scheduler.fetch_pool(type, date)
            rows = db.load(f"pool_{type}", date)
        except db.DomainBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"抓取失败: {type(exc).__name__}: {exc}") from exc
    return {"date": date, "type": type, "rows": _with_broken(rows, type, date)}


def _with_broken(rows: Optional[List[Dict[str, Any]]],
                 kind: str, date: Optional[str]) -> List[Dict[str, Any]]:
    """当日池子 + 断板尾巴。**只有涨停池有断板概念** —— 炸板/跌停/强势的
    「断」在东财自己的字段里（炸板次数、开板次数），另算。"""
    base = rows or []
    if kind != "zt":
        return base
    try:
        return base + board_streak.broken_rows(date)
    except Exception as exc:  # noqa: BLE001  回看挂了不该让整个池子 502
        logger.warning("断板回看失败（%s）: %s", date, exc)
        return base


@router.get("/sector-fund-flow")
def sector_flow(kind: str = Query("industry", pattern="^(industry|concept)$"),
                refresh: bool = False) -> Dict[str, Any]:
    """板块资金流。

    抓取一律走 `scheduler.fetch_sector_flow` —— 它带逐域状态登记与**校验闸门**
    （0 行不许判成功）。原来这里自己 `sector.get_sector_flow` + `db.save`，
    抓到空列表也照存，页面上一片空白而任务记录里写着「成功」—— 这是本阶段
    要收编的两条旁路之一。
    """
    dataset = f"sector_flow_{kind}"
    rows = None if refresh else db.load(dataset, "latest")
    if rows is None:
        try:
            scheduler.fetch_sector_flow(kind)
        except db.DomainBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"板块资金流抓取失败: {exc}") from exc
        rows = db.load(dataset, "latest") or []
    return {"kind": kind, "rows": rows}


@router.get("/news")
def news(
    category: str = Query("all", description="分类 key，见响应里的 categories"),
    refresh: bool = Query(False, description="true=强制重新抓取"),
    limit: int = Query(200, ge=1, le=500),
) -> Dict[str, Any]:
    """财经新闻：六源聚合 + 主题分类 + 热度（进程内缓存 5 分钟）。

    多源本身就是回退——任一源挂掉由其余源补位；全部挂掉时响应 demo=true，
    前端展示内置演示数据。热度是本项目自算的参考值，不构成投资建议。
    """
    payload = news_fetcher.get_news(refresh=refresh)
    known = {"all"} | {c["key"] for c in payload["categories"]}
    if category not in known:
        raise HTTPException(400, f"未知分类: {category}，可选 {sorted(known)}")
    items = [it for it in payload["items"]
             if category == "all" or it["category"] == category]
    return {**payload, "category": category, "items": items[:limit]}


# 打分线程状态（进程内存，重启即清零；结果本身落在 SQLite）
_MF_STATE: Dict[str, Any] = {
    "running": False, "error": "", "started_at": "", "finished_at": "",
    "date": "", "output": 0, "elapsed_sec": None, "degraded": [],
}


@router.get("/main-force/dates")
def main_force_dates() -> Dict[str, Any]:
    """已打分的交易日（供历史回看）。"""
    return {"dataset": "main_force", "dates": mainforce.available_dates()}


# 逐日资金流回补的进程内状态：同一时刻只允许一个线程，失败后 10 分钟冷却
_INFLOW_FILL: Dict[str, Any] = {"running": False, "retry_at": 0.0}
_INFLOW_FILL_BUDGET = (45.0, 60)  # (单次回补最长秒数, 最多只股)


def _start_inflow_backfill(codes: List[str], date: str, days: int) -> None:
    """后台线程补逐日资金流，立即返回。

    放后台而不是在 GET 里同步做：冷启动要逐只打东财 daykline，netfix 的
    1s 最小间隔下 40 只就是 40 秒，同步会让首屏卡死。置位必须在起线程之前，
    否则连续两次请求会开出两个线程（同 /main-force/refresh 的竞态）。

    `STOCK_INFLOW_BACKFILL=false` 时直接不起线程 —— 内网下东财全程 RST，
    回补永远 `fetched=0` 却仍每 10 分钟重试一轮。此时 `missing` 照常返回，
    但 `backfilling` 恒为 false，前端据此停轮询（见 MainForce.tsx）。
    """
    if not settings.inflow_backfill:
        return
    if _INFLOW_FILL["running"] or time.monotonic() < _INFLOW_FILL["retry_at"]:
        return
    _INFLOW_FILL["running"] = True

    def _worker() -> None:
        try:
            stats = mainforce.backfill_inflow(
                codes, date, days,
                budget_sec=_INFLOW_FILL_BUDGET[0],
                max_codes=_INFLOW_FILL_BUDGET[1],
            )
            logger.info("逐日资金流回补完成 %s", stats)
            if stats["fetched"] == 0 and stats["need"] > 0:
                # 一只都没抓到说明源挂了，冷却期内别让每个页面都来空转一轮
                _INFLOW_FILL["retry_at"] = time.monotonic() + 600
        except Exception:  # noqa: BLE001
            logger.exception("逐日资金流回补线程异常")
            _INFLOW_FILL["retry_at"] = time.monotonic() + 600
        finally:
            _INFLOW_FILL["running"] = False

    threading.Thread(target=_worker, daemon=True, name="inflow-backfill").start()


@router.get("/main-force/inflow")
def main_force_inflow(
    date: Optional[str] = Query(None, description="YYYYMMDD，缺省取库内最新"),
    codes: str = Query(..., description="逗号分隔的股票代码，最多 80 个"),
    days: int = Query(5, ge=1, le=10),
) -> Dict[str, Any]:
    """近 `days` 日主力净流入的逐日明细 + 合计（列表列 + 点击弹框共用）。

    **只读 DB，不做网络请求**。覆盖不足时后台起一次回补，并用 `missing`
    告诉前端「还有几只没补齐」，前端轮询到 0 为止 —— 这样首屏立刻有数据
    （哪怕只有 1 天），而不是把 GET 拖成几十秒。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:80]
    if not code_list:
        raise HTTPException(422, "codes 不能为空")

    if date is None:
        date = db.latest_date("main_force") or db.latest_date("stock_net_inflow")
        if date is None:
            raise HTTPException(404, "尚无资金流数据")

    window, missing = mainforce.inflow_gaps(code_list, date, days)
    if missing:
        _start_inflow_backfill(missing, date, days)

    # 每个日期只 load 一次再建索引：80 只 × 5 天直接查会把 4725 行的 JSON
    # 解析 400 遍，实测会把本地接口拖到几百毫秒
    by_date = {
        d: {r.get("code"): r for r in (db.load("stock_net_inflow", d) or [])}
        for d in window
    }
    items: Dict[str, Any] = {}
    for code in code_list:
        series: List[Dict[str, Any]] = []
        total = 0.0
        for d in window:
            row = by_date[d].get(code) or {}
            value = row.get("net_inflow")
            if not isinstance(value, (int, float)):
                continue
            series.append({
                "date": d,
                "net_inflow": float(value),
                "large_order_net": row.get("large_order_net"),
            })
            total += float(value)
        items[code] = {"days": series, "total": total}

    return {
        "date": date,
        "days": days,
        "window": window,
        "items": items,
        "missing": len(missing),
        "backfilling": bool(_INFLOW_FILL["running"]),
    }


@router.get("/main-force")
def main_force(
    date: Optional[str] = Query(None, description="YYYYMMDD，缺省取库内最新"),
    all: bool = Query(False, description="true=附带全部打分记录而非仅 Top N"),
) -> Dict[str, Any]:
    """主力建仓候选 Top N。

    只读缓存，不在此接口里触发计算 —— 单轮计算含 ~300 次逐只日线请求，
    需要数分钟，会让 HTTP 请求超时。没有缓存时引导前端先调 /refresh。
    """
    if date is None:
        date = db.latest_date("main_force")
        if date is None:
            raise HTTPException(
                404, "尚未打分，请先 POST /api/main-force/refresh")

    result = db.load_one("main_force", date)
    if result is None:
        raise HTTPException(404, f"{date} 暂无打分结果，请先触发重算")

    payload = {k: v for k, v in result.items() if k != "all_rows"}
    if all:
        payload["all"] = result.get("all_rows", [])
    return payload


@router.get("/main-force/matrix")
def main_force_matrix(
    days: int = Query(90, ge=1, le=365, description="回看最近多少个交易日"),
) -> Dict[str, Any]:
    """主力建仓「上榜矩阵」：行=股票，列=交易日，格子=当天名次。

    和 `/main-force` 的分工：那个一次给一天（页面上切日期回看单日榜单），
    这个把多天压成一张表，用来看「谁在反复上榜、从哪天开始上、连着几天」。
    每行除 `cells`（上榜格）外还带 `prices`：跑过的日期 → 当日收盘价。

    列取**交易日历**而非「有数据的日期」：中间漏跑的交易日也照样占一列，
    由 missed_dates 单独标出来 —— 「这天没跑任务」和「跑了但没上榜」
    在格子上长得一样，意思完全不同，混在一起会直接读错。

    日历走 cached_trade_dates()：只读本地文件、过期也不联网，
    内网下调外网接口会被安全设备拦截并记录。
    """
    ran_all = db.list_dates("main_force", limit=days)
    if not ran_all:
        return {
            "dates": [], "ran_dates": [], "missed_dates": [],
            "unfillable_dates": [],
            "rows": [], "total": 0, "as_of": "", "days": days,
            "disclaimer": "",
        }

    ran = set(ran_all)
    start = min(ran)

    # end = max(库内最新日, 日历里 <= 今天的最后一个交易日)
    # 后半段让「今天还没跑」也占一列；前半段防日历缓存比库里的数据还旧
    # —— 缓存过期不会自动刷新，因为刷新动作本身会联网。
    today = datetime.now().strftime("%Y%m%d")
    cached = tcal.cached_trade_dates()
    elapsed = [d for d in cached if d <= today]
    end = max(max(ran), elapsed[-1] if elapsed else "")

    col_dates = [d for d in cached if start <= d <= end] if cached else sorted(ran)
    ran_dates = [d for d in col_dates if d in ran]
    missed_dates = [d for d in col_dates if d not in ran]

    # 漏跑日里涨/跌停池已过东财 30 天窗口、本地也没有快照的 —— 补跑按钮对
    # 这些列点了也是 400，得由后端算好告诉前端（前端光看日期算不出「本地
    # 有没有快照」这个例外，按日期一刀切会把还能补的列也藏掉）。
    unfillable_dates = [d for d in missed_dates
                        if mainforce.pool_unavailable_reason(d)]

    # 日期升序遍历，name/industry 被后面的日期覆盖 → 保留最新叫法
    cells: Dict[str, Dict[str, Any]] = {}
    disclaimer = ""
    for d in ran_dates:
        # 只取 rows + disclaimer：payload 里 81% 是矩阵用不到的 all_rows，
        # 90 天逐天全量解析约 20 MB，见 `db.load_one_fields` 的说明。
        block = db.load_one_fields("main_force", d, text_fields=("disclaimer",))
        if not block:
            continue
        disclaimer = block.get("disclaimer") or disclaimer
        for r in block.get("rows") or []:
            if not isinstance(r, dict):
                continue
            code = str(r.get("code") or "").strip()
            rank = r.get("rank")
            if not code or not isinstance(rank, int):
                continue
            rec = cells.setdefault(code, {
                "code": code, "name": "", "industry": "",
                "cells": {}, "hits": 0, "best_rank": 0,
                "last_date": "", "best_score": None,
            })
            score = r.get("score")
            rec["cells"][d] = {
                "rank": rank,
                "score": round(score, 2) if isinstance(score, (int, float)) else None,
            }
            rec["hits"] += 1
            if not rec["best_rank"] or rank < rec["best_rank"]:
                rec["best_rank"] = rank
            rec["last_date"] = d
            if isinstance(score, (int, float)):
                rec["best_score"] = score if rec["best_score"] is None \
                    else max(rec["best_score"], score)
            if r.get("name"):
                rec["name"] = str(r["name"])
            if r.get("industry"):
                rec["industry"] = str(r["industry"])

    # 稳定排序，逐级倒过来排：先定最末位的 best_rank 升序，
    # 再 last_date 降序，最后 hits 降序 —— 同命中数时看谁最近上过、名次更好。
    rows = sorted(cells.values(), key=lambda x: x["best_rank"])
    rows = sorted(rows, key=lambda x: x["last_date"], reverse=True)
    rows = sorted(rows, key=lambda x: x["hits"], reverse=True)

    # 每天的股价：**跑过的**日期列全部给价（含没上榜的日子），单一出处是
    # daily_bar 的不复权收盘价 —— 打分 payload 里那个 price 走的是
    # daily_history(adjust="qfq")，前复权价会随配息改写历史，两套口径混在
    # 同一列里会把除权日读成暴跌。缺本地日线的日期不进 map，前端显 `-`；
    # 漏跑列不给价（那列没有打分，整块走「未跑」的底色）。
    bars = db.bars_load_window("daily_bar", [r["code"] for r in rows],
                               start, end) if rows else {}
    for r in rows:
        seq = bars.get(r["code"]) or {}
        r["prices"] = {
            d: seq[d]["close"] for d in ran_dates
            if isinstance((seq.get(d) or {}).get("close"), (int, float))
        }

    return {
        "dates": col_dates,
        "ran_dates": ran_dates,
        "missed_dates": missed_dates,
        "unfillable_dates": unfillable_dates,
        "rows": rows,
        "total": len(rows),
        "as_of": max(ran),
        "days": days,
        "disclaimer": disclaimer,
    }


@router.get("/main-force/top5-track")
def main_force_top5_track() -> Dict[str, Any]:
    """「疑似主力建仓前5」跟踪名单 + 逐日名次/股价（**零联网**）。

    口径（2026-11-11 拍板）：一个月 = 30 自然日，截止 = 最后一次进前5那天
    + 30 天，重复上榜自动顺延；到期票留在名单标「已到期」、不再拉股价。

    形状对齐 `/main-force/matrix`：`dates`（日历交易日，轴从最早的
    「首次进前5」日起）+ `ran_dates`/`missed_dates` + `rows[].cells/prices`
    —— 前端走势弹框直接复用矩阵那套（蓝 = 不复权收盘价断线、橙 = 名次右轴
    反转）。prices 只给**跑过的**日期，本地日线缺的不进 map、显 `-` 不编数。

    零联网：扫 main_force 快照 + 读 daily_bar，日历走 `cached_trade_dates()`
    只读本地文件（与 matrix 同口径）—— 内网下调外网接口会被拦截并记录。
    """
    rows = top5track.roster()
    if not rows:
        return {
            "dates": [], "ran_dates": [], "missed_dates": [],
            "rows": [], "total": 0, "as_of": "",
            "tracking": 0, "expired": 0, "disclaimer": "",
        }

    ran_all = db.list_dates("main_force", limit=100_000)
    ran = set(ran_all)
    start = min(r["first_date"] for r in rows)

    # end 同 matrix：库里最新日与日历里 <= 今天的最后一个交易日取大，
    # 「今天还没跑」也占一列；日历缓存过期不会自动刷新（刷新会联网）
    today = datetime.now().strftime("%Y%m%d")
    cached = tcal.cached_trade_dates()
    elapsed = [d for d in cached if d <= today]
    end = max(max(ran_all), elapsed[-1] if elapsed else "")

    col_dates = [d for d in cached if start <= d <= end] if cached else sorted(
        {start} | {d for r in rows for d in r["cells"]})
    ran_dates = [d for d in col_dates if d in ran]
    missed_dates = [d for d in col_dates if d not in ran]

    # 股价单一出处 daily_bar 不复权收盘价（同 matrix，取 qfq 会把除权日读成暴跌）
    bars = db.bars_load_window("daily_bar", [r["code"] for r in rows],
                               start, end)
    for r in rows:
        seq = bars.get(r["code"]) or {}
        r["prices"] = {
            d: seq[d]["close"] for d in ran_dates
            if isinstance((seq.get(d) or {}).get("close"), (int, float))
        }

    disclaimer = ""
    latest = db.latest_date("main_force")
    if latest:
        blk = db.load_one_fields("main_force", latest,
                                 text_fields=("disclaimer",))
        disclaimer = (blk or {}).get("disclaimer") or ""

    return {
        "dates": col_dates,
        "ran_dates": ran_dates,
        "missed_dates": missed_dates,
        "rows": rows,
        "total": len(rows),
        "as_of": max(ran_all),
        "tracking": sum(1 for r in rows if r["status"] == "跟踪中"),
        "expired": sum(1 for r in rows if r["status"] == "已到期"),
        "disclaimer": disclaimer,
    }


@router.get("/main-force/status")
def main_force_status() -> Dict[str, Any]:
    """打分线程的实时状态（running / done / error）+ 打分内部进度。

    `progress` 是 `mainforce` 那 7 个阶段与逐只日线的计数（`_PROG`，由打分
    线程写、这里读，各带锁）。**只在本轮真在跑时才给** —— `progress()` 没在
    打分时读到的是上一轮的残留值，闲着也透出去，页面上的进度条会在任务结束后
    继续显示一个不再变化的百分比，看着像卡死。
    """
    out = dict(_MF_STATE)
    out["progress"] = mainforce.progress() if out.get("running") else None
    return out


@router.post("/main-force/refresh")
def main_force_refresh(
    date: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """后台线程重算一轮打分，立即返回，前端轮询 /main-force/status。

    打分走 `scheduler.score_and_capture` —— 与盘后流水线**同一个**任务服务，
    于是它同样登记逐域状态、同样吃校验闸门。原来这里直接调 `mainforce.run()`，
    打完分 `ingest_job` 里查无此域，「最近一次采集」与实际对不上（本阶段要
    收编的另一条旁路）。
    """
    if _MF_STATE.get("running"):
        raise HTTPException(409, "已有一轮打分在进行中")

    # 交易日必须在**起线程之前**定下来：worker 里再推算的话，`_MF_STATE["date"]`
    # 要等十几分钟打分跑完才知道是哪一天，那期间页面上显示的都是空串。
    target = date or scheduler.target_date()
    if not target:
        raise HTTPException(404, "无法确定交易日：交易日历不可用且库内无数据")

    # 涨/跌停池过期的日期，起线程也是白起 —— 0 秒把原因说清楚。
    # 2026-11-02 那次补跑 20260929：烧完 150s 快照 + 2.5 分钟逐只取数，
    # 最后死在跌停池的 30 天闸门上，页面只留一句 traceback 尾行。
    reason = mainforce.pool_unavailable_reason(target)
    if reason:
        raise HTTPException(400, reason)

    busy = db.ingest_busy("main_force", target)
    if busy:
        raise HTTPException(
            409, "main_force @ %s 正在采集中%s" % (
                target, "（开始于 %s）" % busy["started_at"]
                if busy.get("started_at") else ""))

    # 状态必须在**起线程之前**同步置位：放进 worker 里会有竞态，
    # 本接口的返回值和紧接着的第一次 status 轮询可能都还是 false，
    # 前端就会以为任务没启动。顺带清掉上一轮的遗留值。
    _MF_STATE.update({
        "running": True, "error": "",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": "", "date": target,
        "output": 0, "elapsed_sec": None, "degraded": [],
    })

    def _worker() -> None:
        try:
            result = scheduler.score_and_capture(target, date)
            _MF_STATE.update({
                "running": False, "date": result["date"],
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "output": len(result.get("rows", [])),
                "elapsed_sec": result.get("elapsed_sec"),
                "degraded": result.get("model", {}).get("degraded", []),
            })
        except db.DomainBusy as exc:
            # 上面那次预判之后才被别的线程抢走（竞态）。**不是打分失败** ——
            # 数据照样会由那一轮落库，这里只要把 running 清掉、别把页面卡住。
            logger.info("主力建仓打分被并发拦截: %s", exc)
            _MF_STATE.update({
                "running": False, "error": str(exc),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("主力建仓打分失败")
            _MF_STATE.update({
                "running": False, "error": f"{type(exc).__name__}: {exc}",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })

    threading.Thread(target=_worker, name="mainforce-scorer",
                     daemon=True).start()
    return {"status": "started", "date": date, "state": dict(_MF_STATE)}


# ----------------------------------------------------------------- 关注股票


class WatchAdd(BaseModel):
    """关注时把当时的候选快照一起存下来 —— 之后才能回看「为什么当时关注它」。

    `market` 为空时按代码位数自动判（见 `client.market_of`）。
    `source_date` 不传 = 走到「最近一个交易日」—— 手动关注（尤其是港股，
    A 股的候选打分里根本不会出现它）没有天然的建仓日，默认取最新交易日，
    等于「从现在开始跟」。
    """
    code: str = Field(min_length=1, max_length=12)
    market: str = Field(default="", pattern=r"^(|cn|hk)$")
    name: str = ""
    source_date: Optional[str] = Field(default=None, pattern=r"^\d{8}$")
    industry: str = ""
    rank: Optional[int] = None
    score: Optional[float] = None
    price: Optional[float] = None
    snapshot: Optional[Dict[str, Any]] = None


def _attach_perf(items: List[Dict[str, Any]]) -> None:
    """给关注列表逐只算区间表现。带总预算：源卡住时先返回已完成的部分，
    剩下的 perf 为 None，前端显示「-」，可再刷新。"""
    deadline = time.monotonic() + track.FETCH_BUDGET
    for item in items:
        if time.monotonic() > deadline:
            item["perf"] = None
            continue
        try:
            item["perf"] = track.summary(item["code"], item["source_date"],
                                         item.get("market") or "cn")
        except Exception as exc:  # noqa: BLE001  单只失败不能拖垮整个列表
            logger.warning("关注列表算表现失败 %s: %s", item["code"], exc)
            item["perf"] = None


@router.get("/watchlist")
def watchlist(perf: bool = Query(False, description="true=顺带算区间表现")) -> Dict[str, Any]:
    """全部关注股。perf=true 会逐只拉日线，首屏会慢一些，结果有缓存。"""
    items = db.watch_list()
    if perf:
        _attach_perf(items)
    return {"items": items}


@router.post("/watchlist")
def watchlist_add(body: WatchAdd) -> Dict[str, Any]:
    """关注（重复关注同一代码会刷新建仓日与打分快照）。

    三步归一化，缺一不可：

    1. **判市场**：显式 `market` 优先，否则按代码位数（`client.market_of`）；
    2. **代码归一化**：`hk00700` / `700` / `00700` 全部落成 `00700` ——
       直接存原样，后面查日线会拿不到数据；
    3. **港股补名字**：去腾讯 `qt.gtimg.cn` 要一个（约 50ms），失败就用代码，
       **绝不因为查不到名字而关注失败**。
    """
    data = body.model_dump()
    market = data.get("market") or market_of(data["code"])
    code = norm_code(data["code"], market)
    if not code:
        raise HTTPException(400, f"无法识别的代码：{data['code']!r}")
    # 位数是两个市场唯一的硬边界：A 股 6 位、港股 5 位。
    # 卡住它，前端选错市场才会变成一个「看似成功、其实查不到行情」的假关注。
    if len(code) != (5 if market == "hk" else 6):
        raise HTTPException(
            400, f"{data['code']!r} 不是合法的{'港股' if market == 'hk' else 'A股'}"
                 f"代码（{'5' if market == 'hk' else '6'} 位数字）")
    data.update(market=market, code=code)

    if not data.get("source_date"):
        # 手动关注没有天然的建仓日，取最近交易日 = 「从现在开始跟」
        data["source_date"] = (_latest_trade_date()
                               or datetime.now().strftime("%Y%m%d"))

    if market == "hk" and not (data.get("name") or "").strip():
        quote = hk_fetch.quote(code)
        data["name"] = quote.get("name") or code
        if quote.get("price"):
            data["price"] = data.get("price") or quote["price"]

    db.watch_add(data)
    return {"items": db.watch_list()}


@router.delete("/watchlist/{code}")
def watchlist_delete(code: str) -> Dict[str, Any]:
    if not db.watch_remove(code):
        raise HTTPException(404, f"{code} 不在关注列表")
    return {"items": db.watch_list()}


@router.get("/watchlist/{code}/series")
def watchlist_series(
    code: str,
    refresh: bool = Query(False, description="true=忽略缓存重拉"),
) -> Dict[str, Any]:
    """关注股详情：建仓日至今的 K 线 + 区间表现 + 同期基准 + 后续再入选。

    基准由 `market` 决定：A 股 = 沪深300，港股 = 恒生指数。
    """
    item = db.watch_get(code)
    if item is None:
        raise HTTPException(404, f"{code} 未关注")
    try:
        detail = track.detail(code, item["source_date"], refresh=refresh,
                              market=item.get("market") or "cn")
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502, f"行情拉取失败: {type(exc).__name__}: {exc}") from exc
    return {**item, **detail}


# --------------------------------------------------------------- 实时行情
#
# 「实时行情」Tab 的唯一数据入口。**只查请求里带的那些代码**，不扫全市场 ——
# 关注列表十几二十只，一次批量请求就够。
#
# 三块数据的失败互不传染：报价挂了资金流照常返回，分时挂了表格照常显示。
# 所有降级都写进 `warnings`，前端原样摊出来，不做静默吞错。

_LIVE_MAX_CODES = 50


def _live_items(codes: str) -> List[Dict[str, str]]:
    """把 `600519,00700` 解析成 `[{code, market}]`，顺带去重与逐个校验。

    判市场靠 `client.market_of`（按位数）：A 股恒 6 位、港股恒 5 位，
    这是两个市场唯一的硬边界 —— 放过去一位，后面查出来就是另一个代码的东西。
    """
    parts = [c.strip() for c in (codes or "").split(",") if c.strip()]
    if not parts:
        raise HTTPException(400, "codes 不能为空")
    if len(parts) > _LIVE_MAX_CODES:
        raise HTTPException(400, f"一次最多 {_LIVE_MAX_CODES} 只，收到 {len(parts)} 只")
    out: List[Dict[str, str]] = []
    seen = set()
    for raw in parts:
        market = market_of(raw)
        code = norm_code(raw, market)
        if not code or len(code) != (5 if market == "hk" else 6):
            raise HTTPException(400, f"无法识别的代码：{raw!r}")
        if code not in seen:
            seen.add(code)
            out.append({"code": code, "market": market})
    return out


@router.get("/realtime")
def realtime(
    codes: str = Query("", description="逗号分隔的代码，A 股 6 位 / 港股 5 位"),
    detail: str = Query("", description="要带当日分时的那只代码（须包含在 codes 里）"),
) -> Dict[str, Any]:
    """关注股实时报价 + 资金流 + 单只分时。

    分时**只给一只**：表格里十几二十只都要分时就是十几二十次请求，分域限速下
    一轮要跑十几秒；详情面板一次只看得下一幅图，按需取即可。

    字段级校验全在 `fetchers.live`：对不上的字段置 None 并进 `warnings`，
    这里只负责编排，不重新解释数据。
    """
    items = _live_items(codes)
    quote_rows, warns = live_fetch.quotes(items)
    flows, flow_warns = live_fetch.flow([(i["code"], i["market"]) for i in items])

    intr = None
    if detail:
        target = market_of(detail)
        dcode = norm_code(detail, target)
        hit = next((i for i in items if i["code"] == dcode and i["market"] == target), None)
        if hit is not None:
            intr = live_fetch.intraday(hit["code"], hit["market"])

    return {
        "quotes": quote_rows,
        "flows": flows,
        "intraday": intr,
        "warnings": warns + flow_warns,
        "as_of": datetime.now().strftime("%H:%M:%S"),
        "disclaimer": "数据来自公开接口的整理与统计，仅用于数据可视化研究，"
                      "不提供任何买卖信号，不构成投资建议；口径与延迟以数据源为准。",
    }


# --------------------------------------------------------------- 基金估值
#
# 新菜单「基金估值」的数据入口。与 `/realtime` 最大的不同是**手动触发** ——
# 前端点了「查询」才发请求，没有轮询、没有自动刷新，一轮最多 30 只。
#
# 关注列表与股票关注**分表存**（`db.fund_watchlist`）：基金代码恒 6 位、与
# A 股代码格式一模一样但语义完全不同，混在一张表里 `/api/watchlist` 会把
# 基金当股票去查日线。添加时**不联网**，名称等第一次查询估值时回填。

_FUND_MAX_CODES = 30

_FUND_DISCLAIMER = (
    "盘中估值由公开数据估算得出，口径与延迟以数据源为准；未通过校验的字段"
    "一律留空，不做补齐。持仓数据滞后于基金经理实际调仓，估值不代表基金"
    "真实净值，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
)


class FundAdd(BaseModel):
    """基金代码恒 6 位数字，**不带 market** —— 与 A 股 6 位代码撞格式但不是一回事，
    在这里判市场只会把贴错位置的输入静默放过去。"""
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    name: str = ""


@router.get("/fund-watchlist")
def fund_watchlist() -> Dict[str, Any]:
    return {"items": db.fund_watch_list()}


@router.post("/fund-watchlist")
def fund_watchlist_add(body: FundAdd) -> Dict[str, Any]:
    """添加基金。**纯本地写库、不联网** —— 名称留空也照样关注成功。"""
    db.fund_watch_add(body.code, body.name)
    return {"items": db.fund_watch_list()}


@router.delete("/fund-watchlist/{code}")
def fund_watchlist_delete(code: str) -> Dict[str, Any]:
    if not db.fund_watch_remove(code):
        raise HTTPException(404, f"{code} 不在关注列表")
    return {"items": db.fund_watch_list()}


def _fund_items(codes: str) -> List[Dict[str, str]]:
    """`161725,110011` → `[{"code": "161725"}, ...]`，顺带去重。

    **只校验 6 位数字**：基金代码没有市场之分，`_live_items` 里那套按位数判
    市场的逻辑在这里不适用（判了会把 `00700` 这种贴错地方的输入当成港股放行，
    而基金根本没有港股）。
    """
    parts = [c.strip() for c in (codes or "").split(",") if c.strip()]
    if not parts:
        raise HTTPException(400, "codes 不能为空")
    if len(parts) > _FUND_MAX_CODES:
        raise HTTPException(400, f"一次最多 {_FUND_MAX_CODES} 只，收到 {len(parts)} 只")
    out: List[Dict[str, str]] = []
    seen = set()
    for raw in parts:
        if not re.fullmatch(r"\d{6}", raw):
            raise HTTPException(400, f"无法识别的基金代码：{raw!r}（应为 6 位数字）")
        if raw not in seen:
            seen.add(raw)
            out.append({"code": raw})
    return out


# ------------------------------------------------- 涨跌幅那一列显示谁
#
# 用户拍板的口径（2026-10-13）：
#     交易中 → 估算        盘前 → 前一交易日的机构净值
#     盘后   → 当日机构净值出了显示实际，没出才退回估算
#
# 起因是「估值跟实际差 1 个点」没个准话：页面同时摆着估算和净值，两个数
# 都是真的，但没说此刻该信谁。这里把「此刻显示谁」写成一段**可解释**的规则，
# 每行多给三个字段 —— `display_pct`（显示什么）、`display_kind`（来自哪条链）、
# `display_reason`（为什么），前端照着渲染，取不到就说「—」。
#
# 两个输入都**不许联网**：
#   * 时段只看时钟，与前端 `lib/live.ts` 同一套规则（09:30-11:30 / 13:00-15:00）；
#   * 是不是交易日读 `tcal.cached_trade_dates()` 的**本地缓存** —— 缓存缺失
#     返回 None 时不猜，判定退回只看时钟。`/fund-estimate/last` 的「零网络」
#     承诺就是被这条实现守住的：一旦这里去拉日历，首屏就成了发包点。

_NAV_GAP_DAYS = 5   # 净值相邻判定，与 `metrics.fund_review` 同一口径


def _session_state(now: Optional[datetime] = None) -> str:
    """`pre` / `open` / `break` / `closed`，只看时钟（周末照样会说 open）。"""
    cur = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    if 570 <= cur < 690 or 780 <= cur < 900:
        return "open"
    if cur < 570:
        return "pre"
    if cur >= 900:
        return "closed"
    return "break"


def _is_trade_day(day: Optional[str] = None) -> Optional[bool]:
    """`True` / `False`，日历读不到时回 `None`（未知），**绝不联网**。"""
    day = day or datetime.now().strftime("%Y%m%d")
    dates = tcal.cached_trade_dates()
    if not dates:
        return None
    return day in set(dates)


def _local_actuals() -> Dict[str, Tuple[Optional[str], Optional[float]]]:
    """本地净值流水 → `{code: (最新净值日, 该日实际涨跌幅)}`。**零网络**。

    净值流水是每次查询顺手攒的，`estimate()` 那条净值历史链还会一次回填
    最近 40 个净值日，所以这里通常凑得齐前后两日。凑不齐就回 `(日期, None)` ——
    有净值日期也比编一个涨跌幅诚实。
    """
    navs: Dict[str, Dict[str, float]] = {}
    for n in db.fund_navs():
        code, d, v = str(n.get("code") or ""), str(n.get("nav_date") or ""), n.get("nav")
        if not code or not d or isinstance(v, bool):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v != v or v <= 0:
            continue
        navs.setdefault(code, {})[d] = v

    out: Dict[str, Tuple[Optional[str], Optional[float]]] = {}
    for code, series in navs.items():
        dates = sorted(series)
        cur = dates[-1]
        pct: Optional[float] = None
        if len(dates) >= 2:
            prev = dates[-2]
            gap = (datetime.strptime(cur, "%Y-%m-%d")
                   - datetime.strptime(prev, "%Y-%m-%d")).days
            if gap <= _NAV_GAP_DAYS and series[prev] > 0:
                pct = (series[cur] / series[prev] - 1.0) * 100.0
        out[code] = (cur, pct)
    return out


def _attach_display(rows: List[Dict[str, Any]]) -> None:
    """就地补上 `actual_*` 与 `display_*` 三个字段。

    `actual_*` 缺失时先回本地净值流水取 —— 这样 `/fund-estimate/last`（零网络）
    与 `/fund-estimate`（刚联网取过）落在同一套口径上，首屏不会比点完查询
    少一列。**取不到就空着**，绝不拿估算冒充实际。
    """
    session = _session_state()
    is_td = _is_trade_day()
    today = datetime.now().strftime("%Y-%m-%d")
    local: Optional[Dict[str, Tuple[Optional[str], Optional[float]]]] = None

    for r in rows:
        if not r.get("actual_date"):
            if local is None:
                local = _local_actuals()
            got = local.get(str(r.get("code") or ""))
            if got and got[0]:
                r["actual_pct"], r["actual_date"] = got[1], got[0]

        est = r.get("estimate_pct")
        act = r.get("actual_pct")
        adate = r.get("actual_date")

        if is_td is False:
            # 休市日没有「当日」这回事：最近一次公布的净值就是当前的实际
            if act is not None:
                kind, value = "actual", act
                reason = f"非交易日，显示最近公布的机构净值（{adate}）"
            elif est is not None:
                kind, value = "estimate", est
                reason = "非交易日，最近公布的净值未取到，显示上次估算"
            else:
                kind, value, reason = "none", None, "非交易日，估值与净值都没取到"
        elif session == "pre":
            if act is not None:
                kind, value = "actual", act
                reason = f"盘前，显示前一交易日净值（{adate}）"
            elif est is not None:
                kind, value = "estimate", est
                reason = "盘前，前一交易日净值未取到，显示上次估算"
            else:
                kind, value, reason = "none", None, "盘前，估值与净值都没取到"
        elif session == "closed":
            if act is not None and adate == today:
                kind, value = "actual", act
                reason = "已收盘，当日机构净值已公布"
            elif est is not None:
                kind, value = "estimate", est
                reason = "已收盘，当日机构净值尚未公布，显示盘中估算"
            elif act is not None:
                kind, value = "actual", act
                reason = f"已收盘，回退显示最近公布的净值（{adate}）"
            else:
                kind, value, reason = "none", None, "已收盘，估值与净值都没取到"
        else:   # open / break：盘中恒以估算为准
            if est is not None:
                kind, value = "estimate", est
                reason = "交易中，显示盘中估算"
            elif act is not None:
                kind, value = "actual", act
                reason = f"盘中估算未取到，回退显示最近公布的净值（{adate}）"
            else:
                kind, value, reason = "none", None, "交易中，估值与净值都没取到"

        r["actual_pct"] = act
        r["actual_date"] = adate
        r["display_pct"] = value
        r["display_kind"] = kind
        r["display_reason"] = reason


def _stash_nav_series(rows: List[Dict[str, Any]]) -> None:
    """把 `estimate()` 带回的净值序列回填进本地库，然后从行里**摘掉**。

    摘掉是因为一条序列几十个元素，跟着 JSON 走一遍纯属浪费；回填则让下一次
    首屏回读（零网络）能自己算出实际涨跌幅，也顺手把 T+1 复盘的净值侧补齐 ——
    以前必须 D+1 再点一次查询样本才闭合，现在查询当天就能闭合。
    """
    pairs: List[Tuple[str, str, float]] = []
    for r in rows:
        series = r.pop("nav_series", None) or []
        for p in series:
            if isinstance(p, dict) and p.get("date") and p.get("nav") is not None:
                pairs.append((r["code"], p["date"], p["nav"]))
    if pairs:
        db.fund_nav_put_many(pairs)


@router.get("/fund-estimate")
def fund_estimate(
    codes: str = Query("", description="逗号分隔的 6 位基金代码"),
) -> Dict[str, Any]:
    """盘中估值。**每个 code 都会得到一行** —— 两条链都失败时该行全字段为
    null，由 `warnings` 解释原因，前端显示「—」。

    两条链（官方估值 / 持仓穿透）的取数与全部不变量校验都在
    `fetchers.fund_estimate`，这里只做编排、名称回填与**复盘流水落库**。

    落库是本接口唯一的副作用，**纯本地 SQLite、零网络**：把当场算出来的估算按
    (code, 日期) 记一行、把接口带回的真实净值按 (code, 净值日期) 记一行。只有
    拿到有效涨跌幅的行才记 —— 空行记进去只会变成永远闭合不了的 pending。
    这是为了让 T+1 复盘能**被动**攒样本：用户照常点查询，样本自己会攒起来，
    不需要额外做任何事。同日重查覆盖旧值，理由见 `db.fund_estimate_put`。

    每行额外给 `display_pct` / `display_kind` / `display_reason`：按时段决定
    涨跌幅那一列此刻该显示估算还是实际，规则见 `_attach_display` 的注释。
    """
    items = _fund_items(codes)
    rows, warns = fund_fetch.estimate(items)

    # 名称：估值接口给了就回填进库（只补空的，手填的优先），再用库里补缺的
    for r in rows:
        if r.get("name"):
            db.fund_watch_fill_name(r["code"], r["name"])
    stored = {i["code"]: i.get("name") or "" for i in db.fund_watch_list()}
    for r in rows:
        if not r.get("name"):
            r["name"] = stored.get(r["code"], "")

    _record_fund_logs(rows)
    _stash_nav_series(rows)
    _attach_display(rows)

    return {
        "items": rows,
        "warnings": warns,
        "as_of": datetime.now().strftime("%H:%M:%S"),
        "disclaimer": _FUND_DISCLAIMER,
    }


@router.get("/fund-estimate/last")
def fund_estimate_last() -> Dict[str, Any]:
    """**上次**查到的估值，**只读本地 SQLite，一个网络请求都不发**。

    流水本来就每次查询都在写（`_record_fund_logs`，为 T+1 复盘攒样本），缺的
    只是回读这一步 —— 所以页面每次打开都得重新联网查一遍，首屏空着。
    这个接口把那一步补上，于是首屏能直接显示上次的结果，**同时仍然满足
    「不点查询估值就不发包」**：读本地库不算发包。

    返回结构与 `/fund-estimate` 一致（外加 `cached: true`），前端可以复用同一
    张表格。三点必须对用户诚实：

    * `cached: true` —— 前端据此挂「这是上次查询的结果」的提示，**不能把它
      冒充成刚查出来的现值**。
    * `as_of` 是**落库时刻**（不是估值时点），完整到日期，因为数据可能来自几天前。
    * `stale` 按**估算归档日**（`date` 列）算，不是 `gz_time` —— 持仓穿透链的
      `as_of` 只有 `HH:MM:SS` 不带日期，拿它判会把当天的数据误标成陈旧。
      认不出/对不上一律当陈旧，多标一个「非今日」的代价远小于把旧数据当成
      今天的现值看。

    没查过的 code 压根不返回（`fund_last_estimates` 只回有记录的），它在前端
    会落到 `blankRow`，与今天「还没查询」的表现一致；`warnings` 因此留空 ——
    「数据源降级 N 项」的措辞描述的是取数失败，套在「还没查过」头上是错的。
    """
    rows = db.fund_last_estimates()
    today = datetime.now().strftime("%Y-%m-%d")
    stored = {i["code"]: i.get("name") or "" for i in db.fund_watch_list()}

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append({
            "code": r["code"],
            "name": stored.get(r["code"], ""),
            "source": r.get("source") or "",
            "nav": r.get("nav"),
            "nav_date": r.get("nav_date"),
            "estimate_pct": r.get("est_pct"),
            "estimate_nav": r.get("est_nav"),
            "actual_pct": None,
            "actual_date": None,
            "coverage": r.get("coverage"),
            "as_of": r.get("gz_time"),
            "stale": r.get("date") != today,
            "note": None,
        })

    # 零网络：`actual_*` 从本地净值流水里凑，`display_*` 只看时钟 + 本地日历缓存
    _attach_display(items)

    # 落库时刻恒为 `datetime.now().isoformat()`，只把分隔符换成人读的样子
    last = max((r.get("logged_at") or "" for r in rows), default="")
    return {
        "items": items,
        "warnings": [],
        "as_of": last.replace("T", " "),
        "cached": True,
        "disclaimer": _FUND_DISCLAIMER,
    }


# 落库编排已搬到 `store/db.py` —— 14:50 的订阅推送（`app/subscribe.py`）要写
# **同一份**复盘流水，而 `routes` 又依赖 `subscribe`，实现留在这里就会逼得
# `subscribe` 反向 import `routes` 造成循环。
# 名字保持 `_estimate_date` / `_record_fund_logs`：回归脚本 `_review_check`
# 直接引的是 `routes._estimate_date` / `routes._record_fund_logs`。
_estimate_date = db.estimate_date
_record_fund_logs = db.record_fund_logs


_FUND_REVIEW_NOTE = (
    "误差 = 估算涨跌幅 − 实际涨跌幅，**正 = 高估、负 = 低估**。"
    "「实际涨跌幅」由天天基金公布的单位净值前后两日算出，因此这里衡量的是"
    "「本页估算 vs 天天基金净值」，中间还隔着一层数据源。"
    "样本只来自点过「查询估值」的那些日子，是自选样本而非连续序列；"
    "两条链分开统计 —— 持仓穿透只覆盖前十大重仓股中的 A 股部分，系统性偏低是预期的。"
)


@router.get("/fund-review")
def fund_review() -> Dict[str, Any]:
    """T+1 误差复盘。**只读本地 SQLite，一个网络请求都不发** —— 所以前端在
    首屏加载时就能拉，不必等用户点「查询估值」，也不违反「不点不发包」。

    对齐与统计在 `metrics.fund_review.review()`（纯函数，不碰库不联网）。
    """
    names = {i["code"]: i.get("name") or "" for i in db.fund_watch_list()}
    rows, notes = freview.review(db.fund_estimates(), db.fund_navs(), names)
    notes.insert(0, _FUND_REVIEW_NOTE)
    return {
        "items": rows,
        "notes": notes,
        "low_sample": freview.LOW_SAMPLE,
        "disclaimer": _FUND_DISCLAIMER,
    }


# ----------------------------------------------------------- 添加前的模糊搜索
_SEARCH_KINDS = ("fund", "stock")

_SEARCH_NOTE = (
    "这是本页**唯一会联网**的添加步骤：候选来自第三方检索接口，完整性与"
    "准确性以数据源为准，与行情页无关。**添加动作本身仍然只写本地库**——"
    "不搜索、直接手贴 6 位代码也照样能加，搜索取不到不影响添加。"
)


@router.get("/search")
def search(
    q: str = Query(..., description="代码或名称，支持部分匹配"),
    kind: str = Query("stock", description="fund=基金 / stock=股票"),
    market: Optional[str] = Query(
        None, description="仅 kind=stock：cn / hk，子菜单用它限定市场"
    ),
) -> Dict[str, Any]:
    """添加关注前的模糊搜索（**联网**）。

    与 `/watchlist`、`/fund-watchlist` 的「纯本地写库」是两件事 —— 详见
    `fetchers.search` 模块文档。上游**按模糊匹配返回、且查不到时照样回数据**，
    过滤（指数 / 窝轮 / 美股）在 fetcher 里按字段做，不看状态码。
    """
    if kind not in _SEARCH_KINDS:
        raise HTTPException(400, f"kind 只能是 {'/'.join(_SEARCH_KINDS)}")
    if kind != "stock" and market is not None:
        raise HTTPException(400, "market 只对 kind=stock 有效")
    try:
        term = search_fetch.normalize_query(q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    items = (search_fetch.search_stock(term, market) if kind == "stock"
             else search_fetch.search_fund(term))
    return {
        "items": items,
        "q": term,
        "kind": kind,
        "market": market,
        "note": _SEARCH_NOTE,
    }


# ----------------------------------------------------------- 个股日线（详情页）
# 三元组：(拉取时刻, rows, name)。name 只有港股会填 —— A 股的名称由
# 涨跌停池 / 打分给，没必要为了个名字多打一次接口。
_DAILY_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]], str]] = {}
_DAILY_TTL = 1800.0  # 30 分钟
_DAILY_MAX = 200  # 只留最近点开的若干只，别把内存堆起来


def _jsonable(df: Any) -> List[Dict[str, Any]]:
    """DataFrame → JSON 安全的 list[dict]。

    numpy 的 int64/float64 与 NaN/NaT 直接交给 json 会炸，统一换成原生类型与 None。
    """
    if df is None or getattr(df, "empty", True):
        return []
    out: List[Dict[str, Any]] = []
    for row in df.to_dict("records"):
        rec: Dict[str, Any] = {}
        for k, v in row.items():
            if v is None or v is pd.NaT or (isinstance(v, float) and v != v):
                rec[k] = None
            elif hasattr(v, "item"):  # numpy 标量
                rec[k] = v.item()
            else:
                rec[k] = v
        out.append(rec)
    return out


@router.get("/stock/{code}/daily")
def stock_daily(
    code: str,
    days: int = Query(90, ge=5, le=400),
    refresh: bool = Query(False, description="true=忽略缓存重拉"),
) -> Dict[str, Any]:
    """个股日线，给详情页的「走势」图。

    源走 `fetchers.quotes.daily_history`（A 股：东财 → 腾讯；港股：东财 → 新浪，
    两边熔断互不干扰）。**进程内缓存 30 分钟**：详情页是逐只点开的，每次都
    现拉等于把「高频打接口被封」这件事重新造一遍。

    抓不到数据不 5xx —— 返回 rows=[] + error 字段，页面其余信息（池内状态、
    打分、资金流）照常显示，走势图那一格自己降级。
    """
    code = (code or "").strip()
    market = market_of(code)
    code = norm_code(code, market)
    if market == "hk":
        if len(code) != 5:
            raise HTTPException(400, "港股代码必须是 1~5 位数字")
    elif not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, "code 必须是 6 位数字")
    if days > 400 or days < 5:
        raise HTTPException(400, "days 需在 5~400 之间")

    key = f"{market}:{code}:{days}"
    now = time.time()
    # 港股的名称优先用关注记录里的（不花网络），没有再问腾讯
    name = ((db.watch_get(code) or {}).get("name") or "") if market == "hk" else ""

    hit = _DAILY_CACHE.get(key)
    if hit and not refresh and now - hit[0] < _DAILY_TTL:
        return {"code": code, "market": market, "name": hit[2], "days": days,
                "rows": hit[1], "cached": True, "age_sec": int(now - hit[0])}

    payload: Dict[str, Any] = {
        "code": code, "market": market, "name": name, "days": days, "rows": [],
        "cached": False, "age_sec": 0,
    }
    try:
        if market == "hk":
            # 港股不套 A 股交易日历：两边休市日不同（国庆 A 股停 7 天、
            # 港股通常只停 10 月 1 日），硬套 prev_trade_date 会算错窗口。
            # 改成按自然日回推、多取约 60%，取回来再截断到 days 根。
            today = datetime.now()
            end = today.strftime("%Y%m%d")
            start = (today - timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d")
        else:
            # last_trade_date 给的是 YYYYMMDD 字符串，prev_trade_date 吃的是 date ——
            # 直接把字符串塞进去会在 strftime 上炸
            end = tcal.last_trade_date()
            end_d = datetime.strptime(end, "%Y%m%d").date()
            start = tcal.prev_trade_date(end_d, n=max(1, days - 1))
        df = quotes.daily_history(code, start, end, market=market)
        if market == "hk" and df is not None and len(df) > days:
            df = df.tail(days).reset_index(drop=True)  # 新浪给的是全量
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股日线失败 %s(%s): %s", code, market, exc)
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    rows = _jsonable(df)
    payload["rows"] = rows
    if market == "hk" and not name:
        # 没关注过就现问腾讯（约 50ms，进程内 30 分钟缓存兜住重复点开）
        payload["name"] = hk_fetch.quote(code).get("name") or ""
    if not rows:
        payload["error"] = "日线源暂不可用（东财与备源都没回数据），稍后再试"
        return payload
    if len(_DAILY_CACHE) >= _DAILY_MAX:
        _DAILY_CACHE.pop(next(iter(_DAILY_CACHE)), None)
    _DAILY_CACHE[key] = (now, rows, payload.get("name") or "")
    return payload


@router.post("/refresh")
def refresh(date: Optional[str] = None) -> Dict[str, Any]:
    """手动触发一次**轻量**抓取（涨跌停池 + 板块资金流），同步返回。

    不含打分。要跑含打分的完整流水线用 POST /jobs/run（后台，约 12-15 分钟）。
    """
    date = date or _latest_trade_date() or ""
    if not date:
        raise HTTPException(400, "无法确定交易日，请显式传入 date=YYYYMMDD")
    try:
        return scheduler.run_light(date_str=date, kind="manual_light")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"抓取失败: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------- 数据管理


class DataPurgeReq(BaseModel):
    """数据清理请求。

    设计上把「会不会写库」拆成两个显式字段：`dry_run` 默认 `True`，
    真删时还必须带 `confirm="DELETE"` —— 两道门，少一道都执行不了。
    """

    scope: str = Field(
        description="snapshot | retention | job_run | vacuum | log")
    dataset: Optional[str] = Field(None, description="scope=snapshot 必填")
    date: Optional[str] = Field(None, description="YYYYMMDD，删这一天")
    before: Optional[str] = Field(None, description="YYYYMMDD，删这一天之前的")
    log_name: Optional[str] = Field(None, description="scope=log 时的日志名（白名单）")
    unfinished_only: bool = Field(True, description="scope=job_run：只清未收尾的")
    dry_run: bool = Field(True, description="默认 True —— 不确认绝不写库")
    confirm: str = Field("", description='真删时必须等于 "DELETE"')


@router.get("/data/usage")
def data_usage() -> Dict[str, Any]:
    """数据占用画像 + 保留策略。**只读**：不写库、不发任何网络请求。

    给三件事：库里到底占了多少（库文件 / 各表 / 按 dataset 分组 / 日志文件）、
    有多少可回收（`auto_vacuum=0`，删完得显式 VACUUM 才真缩）、
    以及每个 dataset 的**硬下限** —— 删到那以内会破坏矩阵回看、日期下拉与打分
    窗口，删除接口据此拒绝，见 `db.DATASET_FLOOR`。

    `kv` 只回键名：里面有飞书 App Secret 与 webhook 签名密钥。
    """
    out = db.usage()
    out["logs"] = logs.list_logs()
    out["inflow_backfill"] = settings.inflow_backfill
    return out


@router.post("/data/purge")
def data_purge(req: DataPurgeReq) -> Dict[str, Any]:
    """执行数据清理。**默认 dry_run**，真删必须 `dry_run=false` + `confirm="DELETE"`。

    | scope | 作用 | 保护 |
    |---|---|---|
    | `snapshot` | 按 dataset + date/before 删行情快照 | 不低于 `DATASET_FLOOR` 的日期拒删；`sector_flow_*` 整类不可删 |
    | `retention` | 按 `settings.retention_*` 逐 dataset 清过期数据 | 实际保留取 `max(retention, FLOOR)`；`sector_flow_*` 不进计划 |
    | `job_run` | 清未收尾残留 + 超出 `job_run_keep` 的旧记录 | 默认只清未收尾 |
    | `vacuum` | 回收 freelist 空间 | 无预览，必须真跑 |
    | `log` | 原地截断日志 | 名字走白名单，不能传路径 |

    `kv`（凭据）与 `watchlist`（用户数据）不在任何清理范围内。
    """
    if not req.dry_run and req.confirm != "DELETE":
        raise HTTPException(400, 'dry_run=false 时必须带 confirm="DELETE"')

    if req.scope == "snapshot":
        out = db.purge_snapshot(dataset=req.dataset, date=req.date,
                                before=req.before, dry_run=req.dry_run)
    elif req.scope == "retention":
        out = db.purge_by_retention(dry_run=req.dry_run)
    elif req.scope == "job_run":
        out = db.purge_job_runs(unfinished_only=req.unfinished_only,
                                dry_run=req.dry_run)
    elif req.scope == "vacuum":
        if req.dry_run:
            out = {"dry_run": True, "applied": False,
                   "hint": "VACUUM 没有可预览的内容，真跑需 dry_run=false + confirm"}
        else:
            out = {"dry_run": False, "applied": True, **db.vacuum()}
    elif req.scope == "log":
        # 白名单在**预览阶段**就校验：否则 dry_run 会静默返回 target=null，
        # 调用方以为这个日志不存在，其实只是名字不在白名单里。
        if req.log_name not in logs.LOG_FILES:
            raise HTTPException(
                422, f"不允许操作的日志：{req.log_name}"
                     f"（白名单：{sorted(logs.LOG_FILES)}）")
        if req.dry_run:
            hit = next((x for x in logs.list_logs()
                        if x["name"] == req.log_name), None)
            out = {"dry_run": True, "applied": False, "target": hit,
                   "hint": "截断不可恢复，真跑需 dry_run=false + confirm"}
        else:
            out = {"dry_run": False, **logs.truncate_log(req.log_name or "")}
    else:
        raise HTTPException(422, f"未知 scope：{req.scope}")

    if isinstance(out, dict):
        # 回带 scope：vacuum 与 log 的返回字段会重叠（都有 before/after），
        # 前端不带 scope 就分不清刚才执行的是哪一项，文案会串。
        out["scope"] = req.scope
        if out.get("error"):
            raise HTTPException(422, out["error"])
    return out


# ------------------------------------------------------------------- 研究
#
# 历史回测与策略研究（docs/backtest/）。整段有三条贯穿约束：
#
#   1) **零联网** —— 全部是本地 SQLite 读写。库里缺什么 audit 就报什么
#      （日线与指数各查各的表，BLOCKED/PARTIAL 如实给），绝不"顺手去取一下"。
#   2) **按字段取数、分页返回** —— 实测一条 main_force 快照 243 KB，整条
#      读只有 214 ops/s，按字段取有 870；锁本身只占 0.27 ms。列表端点
#      一律带 limit/offset 并回报 `has_more`/`truncated`，不静默丢数据。
#   3) **跑任务不阻塞请求** —— 实验运行放后台线程，进度落库（重启也不丢），
#      页面轮询 `/research/runs/{id}`。


class ResearchCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80, description="实验名")
    strategy_version: str = Field("sixdim-v1", max_length=40,
                                  description="模型版本，入档以便追溯")
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description="冻结的实验参数：weights / horizons / portfolios / "
                    "cost / ablation")
    universe_mode: str = Field(
        "current", description="current=当前股票池回看（模式 A）；"
                               "historical 当前被显式拒绝")
    start_date: str = Field(..., pattern=r"^\d{8}$", description="起始日 YYYYMMDD")
    end_date: str = Field(..., pattern=r"^\d{8}$", description="结束日 YYYYMMDD")
    data_snapshot_id: str = Field("", max_length=200)


@router.get("/research/capabilities")
def research_capabilities() -> Dict[str, Any]:
    """这次安装**当前能做什么 / 不能做什么**，给前端建实验表单用。

    与其让页面写死一堆选项、等用户提交后才被 400 打回来，不如把可选项、
    默认值和**禁用项的理由**一次给全。模式 B 也在这里明说不可用及原因，
    而不是在下拉框里灰掉却没有解释。
    """
    return {
        "horizons": list(engine.HORIZONS),
        "portfolios": list(engine.PORTFOLIOS),
        "sort_version": engine.SORT_VERSION,
        # 交易所规则事实，不是估计值。
        "limit_ratio": {"主板": 0.10, "创业板": 0.20, "科创板": 0.20,
                        "北交所": 0.30},
        "universe_modes": {
            "current": {"enabled": True,
                        "caveat": engine.SURVIVORSHIP_NOTE},
            db.UNIVERSE_MODE_HISTORICAL: {
                "enabled": False,
                "reason": db.UNIVERSE_HISTORICAL_BLOCK_REASON},
        },
        # 成本默认**不配**；这组只是可选预设，且自带"研究假设"标记。
        "cost_preset": {
            "values": {k: v for k, v in engine.DEFAULT_COST.items()},
            "note": "研究假设，不等于实际券商费率；不选它则可执行收益留空。",
        },
        "blocked": {
            "full_six_dim_backtest": (
                "5/6 因子依赖的日线不落库，且 qfq 前复权会回溯污染历史价格"
                "（docs/backtest/implementation-audit.md §3）"),
            "benchmark": ("超额收益取自**库里**的沪深300（index_daily 的 sh000300，"
                          "与 audit 同判据）；基准覆盖不到的信号日留空，绝不填 0"),
        },
        "audit": "/api/research/audit",
    }


@router.get("/research/experiments")
def research_experiments() -> Dict[str, Any]:
    """实验列表（新的在前）。**不带** config_snapshot —— 列表页用不上，
    每条都解一遍 JSON 是白扔的时间。"""
    return {"items": db.research_experiment_list()}


@router.post("/research/experiments")
def research_experiment_create(body: ResearchCreateBody) -> Dict[str, Any]:
    """建实验并冻结配置。

    校验不过就 400：未知股票池模式、模式 B（historical）、日期倒挂都在
    `db.research_experiment_create` 当场拦。**模式 B 的拒绝理由会原样
    回给调用方**，这样前端不用自己复述一遍为什么不能选。
    """
    if body.end_date < body.start_date:
        raise HTTPException(400, "结束日不能早于起始日")
    try:
        eid, digest = db.research_experiment_create(
            body.name, body.strategy_version, body.config,
            body.universe_mode, body.start_date, body.end_date,
            body.data_snapshot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"experiment": db.research_experiment_get(eid),
            "config_hash": digest}


@router.get("/research/experiments/{eid}")
def research_experiment_detail(eid: int) -> Dict[str, Any]:
    """单个实验：行本身 + 冻结的配置 + 运行历史。

    配置随行返回，是因为"这个实验当时到底按什么参数跑的"是复现的第一问，
    再点一次接口去查就没人查了。
    """
    exp = db.research_experiment_get(eid)
    if exp is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    return {"experiment": exp,
            "config_hash": exp.get("config_hash"),
            "runs": db.research_runs_of(eid)}


@router.post("/research/experiments/{eid}/run")
def research_experiment_run(eid: int) -> Dict[str, Any]:
    """开一轮运行（后台线程）。

    重复点不会变成两条并行任务 —— `research_run_create` 在同一把锁里拒绝
    已有 pending/running 的实验。运行状态写在库里，进程重启也不丢。
    """
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    try:
        rid = db.research_run_create(eid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    threading.Thread(target=_run_research, args=(eid, rid),
                     daemon=True, name=f"research-{eid}-{rid}").start()
    return {"run": db.research_run_get(rid)}


def _run_research(eid: int, rid: int) -> None:
    """后台跑一轮。异常由引擎自己落库，这里只兜底打日志。"""
    try:
        engine.BacktestEngine(eid, rid).run()
    except Exception:  # noqa: BLE001  引擎已落库，这里只是别让线程静默死掉
        logger.exception("研究实验 %s 运行 %s 异常退出", eid, rid)


@router.get("/research/experiments/{eid}/runs")
def research_experiment_runs(eid: int) -> Dict[str, Any]:
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    return {"items": db.research_runs_of(eid)}


@router.get("/research/runs/{rid}")
def research_run_detail(rid: int) -> Dict[str, Any]:
    """运行状态（进度 / 阶段 / 警告），给前端轮询。"""
    run = db.research_run_get(rid)
    if run is None:
        raise HTTPException(404, f"运行不存在：{rid}")
    return {"run": run}


@router.post("/research/runs/{rid}/cancel")
def research_run_cancel(rid: int) -> Dict[str, Any]:
    """取消一轮未完成的运行。

    取消是**协作式**的：引擎在每个阶段边界和长循环里查状态，所以不会立刻
    停下，但一定会停下；已写入的信号保留、指标不写 —— 半成品不冒充完整结果。
    """
    if db.research_run_get(rid) is None:
        raise HTTPException(404, f"运行不存在：{rid}")
    return {"cancelled": db.research_run_cancel(rid),
            "run": db.research_run_get(rid)}


@router.get("/research/experiments/{eid}/signals")
def research_experiment_signals(
        eid: int,
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    """信号列表。分页而不是整表搬运；`has_more` 明说还有没有下一页。"""
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    items = db.research_signals_of(eid, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset,
            "has_more": len(items) >= limit}


@router.get("/research/experiments/{eid}/outcomes")
def research_experiment_outcomes(
        eid: int,
        horizon: Optional[int] = Query(None, ge=1,
                                       description="只看某个观察期，留空=全部"),
        limit: int = Query(1000, ge=1, le=10000)) -> Dict[str, Any]:
    """逐条结果。结果行数 = 信号数 × 观察期数，**必须**按观察期过滤或截断，
    否则会把整条链拖慢（见本节开头的实测数字）。"""
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    items = db.research_outcomes_of(eid, horizon=horizon, limit=limit)
    return {"items": items, "horizon": horizon, "limit": limit,
            "truncated": len(items) >= limit}


@router.get("/research/experiments/{eid}/metrics")
def research_experiment_metrics(eid: int) -> Dict[str, Any]:
    """汇总指标（组合 × 观察期）。

    每行都带 `quality_notes`：幸存者偏差、缺基准、样本量不足、为何没有
    最大回撤 —— 这些**必须**和数字一起返回，不能只放在文档里。
    """
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    return {"items": db.research_metrics_of(eid)}


@router.delete("/research/experiments/{eid}")
def research_experiment_delete(eid: int) -> Dict[str, Any]:
    """删除实验，**级联**清掉它的运行、信号、结果、指标（一次事务）。

    拒绝分三档：不存在 404；引擎线程还在收尾 409（协作式取消后线程要过
    一会儿才退出，立刻删会留下它随后写回的孤儿行）；还有未完成的运行
    409 并告知先取消。删掉的行数逐表回报，可与删前的数对账。
    """
    if db.research_experiment_get(eid) is None:
        raise HTTPException(404, f"实验不存在：{eid}")
    if any(t.name.startswith(f"research-{eid}-") and t.is_alive()
           for t in threading.enumerate()):
        raise HTTPException(409, f"实验 {eid} 的运行线程还在收尾，稍候再删（可先取消）")
    try:
        deleted = db.research_experiment_delete(eid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": deleted}


@router.get("/research/audit")
def research_audit() -> Dict[str, Any]:
    """数据可回测性审计报告。

    故意不缓存 —— 库在变，缓存会让页面停在上一次的结论上。整份报告是
    本地 SQLite 的只读查询，一次外发都没有。
    """
    try:
        return BacktestDataAuditService().audit()
    except Exception as exc:  # noqa: BLE001
        logger.error("数据审计失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"数据审计失败: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------- 采集覆盖（管线）


@router.get("/pipeline/coverage")
def pipeline_coverage_report() -> Dict[str, Any]:
    """采集覆盖审计 —— 每个数据域「采到哪天、缺什么、上次什么时候、错在哪」。

    与 `/research/audit` 的关系：**这里是事实，那里是判定**。
    本端点只报库里有什么；`/research/audit` 在这些事实上做「能不能回测」的
    二次判定。两边共用 `app/pipeline/coverage.py`，同一个数字不会两处各算
    一遍、各说各话。

    **零联网、只读。** 交易日历读本地缓存，过期就照实说「过期了」，
    不会为拿一份新日历去外发一次请求 —— 内网环境下那会被公司安全设备
    拦截并留下记录。

    `dataset` 可选：只查一个域（页面按域展开时用），不传则全量。
    """
    try:
        report = pipeline_coverage.coverage_report()
    except Exception as exc:  # noqa: BLE001
        logger.error("覆盖审计失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"覆盖审计失败: {type(exc).__name__}: {exc}") from exc

    # `per_day_items` 逐日条数是给页面画条形图/排薄日子用的，全量返回会让
    # payload 在域多、日期多时膨胀。这里只在报告里留「峰值 + 薄日」，
    # 逐日明细走下面那个专用端点，按需取。
    slim: Dict[str, Any] = dict(report)
    slim["datasets"] = {
        k: {kk: vv for kk, vv in f.items() if kk != "per_day_items"}
        for k, f in report["datasets"].items()
    }
    return slim


@router.get("/pipeline/coverage/{dataset}")
def pipeline_dataset_detail(dataset: str,
                            limit: int = 60) -> Dict[str, Any]:
    """单个数据域的逐日明细（条数、缺字段数）。

    `limit` 是**最近多少个交易日**，不是第几页 —— 页面要看的从来都是
    「最近这段怎么样」，从头翻 1990 年没有操作价值。
    """
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit 需在 1..500 之间")
    try:
        conn = pipeline_coverage.open_ro()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500, f"库无法只读打开: {type(exc).__name__}: {exc}") from exc
    try:
        if "snapshot" not in pipeline_coverage._tables(conn):
            raise HTTPException(404, "库里没有 snapshot 表")
        rows = conn.execute(
            "SELECT trade_date, row_count, source, fetched_at, payload "
            "FROM snapshot WHERE dataset=? AND trade_date<>'latest' "
            "ORDER BY trade_date DESC LIMIT ?", (dataset, limit)).fetchall()
        items: List[Dict[str, Any]] = []
        for r in rows:
            got = pipeline_coverage._rows_of(dataset, r["payload"])
            total, bad = pipeline_coverage._missing_field_count(dataset, got)
            items.append({
                "trade_date": r["trade_date"],
                "row_count": r["row_count"],
                "source": r["source"],
                "fetched_at": r["fetched_at"],
                # payload 里实际解析出多少行、其中几行缺必填字段。
                # 与 row_count 不一致本身就说明入库和落库对不上。
                "parsed_rows": total,
                "missing_field_rows": bad,
            })
        items.reverse()          # DESC 取、正序给页面
        return {"dataset": dataset, "items": items}
    finally:
        conn.close()


@router.get("/pipeline/gaps")
def pipeline_gaps(dataset: Optional[str] = None,
                  lookback: int = 30) -> Dict[str, Any]:
    """近期漏采速查 —— 最近 N 个已过期交易日里，哪些没采到。

    **只看已过期的交易日**：今天还没走完、明天还没到，都不算漏采。
    也不把「1990 年以来从没采过」列出来 —— 那对补跑没有操作价值，
    看完整历史缺口走 `/pipeline/coverage`。
    """
    if lookback < 1 or lookback > 250:
        raise HTTPException(400, "lookback 需在 1..250 之间")
    try:
        out = pipeline_coverage.gaps(dataset=dataset, lookback=lookback)
    except Exception as exc:  # noqa: BLE001
        logger.error("漏采检查失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"漏采检查失败: {type(exc).__name__}: {exc}") from exc
    return {
        "lookback": lookback,
        "gaps": out,
        # 恒为 True 是设计：本端点一次外发都没有。
        "offline": True,
    }


@router.get("/pipeline/bars")
def pipeline_bars(scope: str = "all", limit: int = 20) -> Dict[str, Any]:
    """历史日线 + 复权因子的落库事实，以及**回填跑到哪了**。零联网、只读。

    与 `/pipeline/coverage` 的分工：那个审 `snapshot`（当日/逐日快照的采集），
    这个审 `daily_bar` / `index_daily`（跨年历史行情）。两张表的判据完全不同
    —— 快照看「哪几个交易日缺了」，历史行情看「哪几只票的行数还没齐」。
    合成一个端点会让「缺失」有两种含义挤在同一张表里。

    进度判据**只看行数**：表的交易日是全表共有的集合，某票行数 == 交易日
    总数就等价于「每天都齐」。少一天计 partial、一行都没有计 absent，
    两种都不是「已完成」—— 这正是任务书要求的「未经验证的数据不得标记为完整」。

    :param scope: 名单范围，见 `backfill.SCOPES`（all/mf/pool/wl）
    :param limit: 最多列出多少只「还没齐」的票，缺日期明细也按这个上限给

    `incomplete` 只列前 N 只（按缺的天数降序）：4813 只全列出来是一堵墙，
    而人要看的从来是「最缺的那批」。要全量去跑 `/pipeline/bars` 的底层事实。
    """
    if scope not in backfill.SCOPES:
        raise HTTPException(
            400, f"scope 需在 {backfill.SCOPES} 之间，收到 {scope!r}")
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit 需在 1..200 之间")

    try:
        tables = {
            t: {"coverage": db.bars_coverage(t),
                "factor": db.bars_factor_coverage(t)}
            for t in db.BAR_TABLES
        }
        bars = tables["daily_bar"]["coverage"]
        days = int(bars["days"] or 0)
        codes = backfill.load_scope(scope) if scope else []
        counts = db.bars_code_counts("daily_bar")
    except Exception as exc:  # noqa: BLE001
        logger.error("行情覆盖查询失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"行情覆盖查询失败: {type(exc).__name__}: {exc}") from exc

    prog: Dict[str, Any] = {
        "scope": scope,
        "codes": len(codes),
        "days": days,
        "window": {"start": bars["first_date"], "end": bars["last_date"]},
        "covered": 0,
        "partial": 0,
        "absent": 0,
        # None 而不是 0：表是空的 / 名单是空的，说「进度 0%」一样是编数字
        "rate": None,
        "incomplete": [],
        "note": "",
    }

    if days == 0:
        prog["note"] = "daily_bar 里一行都没有，谈不上进度。"
    elif not codes:
        prog["note"] = (f"名单范围 {scope} 是空的 —— 库里没有可比对的股票名单，"
                        "进度无法判定（不等于已完成）。")
    else:
        table_dates = set(db.bars_dates("daily_bar"))
        full = [(c, counts.get(c, 0)) for c in codes if counts.get(c, 0) >= days]
        part = [(c, counts.get(c, 0)) for c in codes
                if 0 < counts.get(c, 0) < days]
        absent = [c for c in codes if counts.get(c, 0) == 0]
        prog["covered"] = len(full)
        prog["partial"] = len(part)
        prog["absent"] = len(absent)
        prog["rate"] = round(len(full) / len(codes), 4)

        # 缺的天数降序 —— 最缺的排前面才是有人要看的顺序
        part.sort(key=lambda x: (x[1], x[0]))
        prog["incomplete"] = [
            {"code": c, "have": have, "missing": days - have,
             "missing_dates": sorted(table_dates - set(
                 db.bars_dates("daily_bar", c)))}
            for c, have in part[:limit]
        ]
        prog["note"] = ("covered = 名单里该表区间内每天都齐的票；"
                        "partial = 缺日子；absent = 一行都没有。"
                        "只有 covered 才算完成。"
                        + ("partial 里可能混着停牌股：库里没有停牌表，"
                           "缺的日期判不出是「没抓」还是「当天没交易」，"
                           "所以一律按缺报 —— 宁可少报完成，也不多报。"
                           if part else ""))

    return {
        "offline": True,
        "progress": prog,
        "tables": tables,
        # 因子这一列是给页面提示「收益能不能判复权」用的，不参与进度判定。
        "note": ("因子覆盖率低 ≠ 行情不完整，只是这段行情里有多少行判得清除权"
                 "（见 docs/backtest/limitations.md §6）。"),
    }


@router.get("/pipeline/health")
def pipeline_health() -> Dict[str, Any]:
    """最近一次成功交易日 + 各域数据质量状态 + 到点待补跑条数。

    **零联网，只读 `ingest_job` + kv。** 与 `/api/pipeline/ingest` 的分工：那个
    给逐行明细（哪一天哪一域），这个给一个数能直接放进页面顶栏的结论 ——
    「最近一次成功是哪天、现在有几条到点、上次定时检查是什么时候」。

    ⚠️ `retry_due` 只是**报告**：本端点与定时触发点都不跑补跑，真跑仍然只有
    人点「补跑重试」那一条路（见 `scheduler._retry_fire`）。
    """
    try:
        return jobstate.health_report()
    except Exception as exc:  # noqa: BLE001
        logger.error("读取采集健康状态失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"读取采集健康状态失败: {type(exc).__name__}: {exc}") from exc


@router.get("/pipeline/ingest")
def pipeline_ingest(domain: Optional[str] = None, limit: int = 100,
                    due_only: bool = False) -> Dict[str, Any]:
    """逐域采集状态：每个 (数据域, 交易日) 的尝试次数、落库数、错误、下次重试。

    与 `/api/jobs/status` 的分工：那个说「这一轮流水线跑完了没」（整轮一条），
    这个说「pool_zt 那一天到底怎么了」（逐域逐日一行）。两者都在，缺一个都会
    让「上周三为什么缺数据」这个问题回答不了。

    `due_only=true` 只回**到点该重试**的那些行 —— 没到点的捞出来等于绕过退避，
    源挂着的时候会被反复打。
    """
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit 需在 1..500 之间")
    try:
        items = db.ingest_due() if due_only else db.ingest_list(domain, limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("读取采集状态失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"读取采集状态失败: {type(exc).__name__}: {exc}") from exc
    # `status_alias` 是同一份状态的**任务书用词**（pending / running /
    # success / partial_success / failed）。库内那套大写枚举照旧一起给 ——
    # 前端还按它打标签，两个字段是同一件事的两种说法，不是两套状态机。
    for it in items:
        it["status_alias"] = jobstate.api_status(it.get("status"))
    return {
        "items": items,
        "count": len(items),
        # 这条也是零联网：整张表都是本地库里的记录。
        "offline": True,
        "note": ("status=PARTIAL 表示**跑成了但数据不能信**（源没抛异常，"
                 "但行数为 0 或字段缺失）；FAILED 才是没跑成。"
                 "status_alias 是同一份状态的任务书用词。"),
    }


@router.post("/pipeline/collect/retry")
def pipeline_collect_retry(domain: str = "") -> Dict[str, Any]:
    """把**到点**的采集重试逐条跑掉。

    ⚠️ **本端点会联网** —— 它是「补跑」的入口，不是查询。只在人明确点了
    「补跑重试」时才调；服务启动、页面刷新、任何定时器都**不会**碰它。

    :param domain: 只补这**一个**数据域（`pool_zt` 这种），空 = 全部到点的。
        这就是页面上「指定数据域」那个入口。无论填不填，跑的都**只有已经
        到点的**那些行 —— 挑域不绕过退避，源挂着的时候不会被反复打。

    默认值刻意用裸 `""` 而不是 `Query(default="")`：后者在被当普通 Python
    函数直接调用时（回归测试就是这么调的）会留下一个 `Query` 哨兵，而
    `if domain:` 判它为真 —— 于是空参数被当成「指定了一
    个不存在的域」，一次都不跑。

    同步执行：逐条都是秒级的池子/板块请求（不含打分那十几分钟）。真要跑
    带打分的完整补跑，用已有的 `POST /api/jobs/run`。
    """
    try:
        out = scheduler.run_retries(domain=domain or None)
    except Exception as exc:  # noqa: BLE001
        logger.error("采集重试失败: %s", exc, exc_info=True)
        raise HTTPException(
            500, f"采集重试失败: {type(exc).__name__}: {exc}") from exc
    return out


# ------------------------------------------------- 六个每日任务（阶段 3）
#
# 任务书要「六个任务各自能单独触发、单独重跑」，但**入口只有一个**：页面按钮与
# 将来阶段 4 的定时触发都打 POST /pipeline/tasks/{task_id}。第二个入口（每个页面
# 各起一个端点）迟早漂移 —— 改了任务忘了改按钮，就会出现「重跑失败、按钮显示
# 成功」这种两边各自都对、合起来错的不一致。


@router.get("/pipeline/tasks")
def pipeline_tasks() -> Dict[str, Any]:
    """六个任务的定义与各自最近一次运行。**零联网，只读 job_run。**"""
    return tasksvc.status_report()


@router.post("/pipeline/tasks/{task_id}")
def pipeline_tasks_run(task_id: str,
                       payload: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """单独跑一个任务。

    ⚠️ **六个里有四个会联网**（行情快照 / 资金流排名 / 补跑 / 历史回填），其中
    历史回填是**几千次请求量级**。只在人明确点了按钮时才调；服务启动、页面刷新、
    任何定时器都不会碰它。

    :body: 任务自己的参数 —— ``domain`` / ``start`` / ``end`` / ``items`` /
        ``budget`` / ``trade_date``。交易日缺省由 ``scheduler.target_date()``
        定（今天是交易日取今天，否则取最近交易日 —— 周末手动触发也落在真实
        交易日上）。

        **多给一个参数直接 400**，不静默忽略：漏掉一个拼错的 ``budget``，等于
        把「这次只许发 50 次请求」变成「不限量」。

    :return: ``{task_id, trade_date, status, started_at, finished_at,
                elapsed_sec, offline, job_id, result}``

    400 = 参数给错了（未注册的任务 / 多给的键 / 区间反了 / 名单是空的），
    500 = 任务真的跑挂了。**两种都会在 job_run 里留一条 failed** —— 接口报错而
    库里显示成功这种事，不允许发生。
    """
    params = {k: v for k, v in (payload or {}).items() if v is not None}
    try:
        return tasksvc.run_task(task_id, **params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("任务 %s 失败: %s", task_id, exc, exc_info=True)
        raise HTTPException(
            500, f"任务失败: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------- 历史回填（阶段 4）
#
# 三个端点一条链：先看能补什么（零联网）→ 看要发多少请求（零联网）→ 才动手。
# 后两个都显式标 `offline`，前端据此决定要不要拦一道确认 —— 补历史是一次会
# 打几千次网络请求的批量动作，绝不能让人手滑点一下就开跑。


@router.get("/pipeline/backfill/domains")
def pipeline_backfill_domains() -> Dict[str, Any]:
    """哪些数据**能**回填、哪些**明确不能**、为什么不能。

    `refused` 这一半是设计要求，不是凑数：当日数据（涨停池、个股当日资金流）
    事后公开接口**取不回**，工具必须把这件事说清楚。返回空清单会让调用方
    误以为「补过了，本来就没有」—— 那是把「不知道」包装成了「知道」。
    """
    from app.pipeline import backfill
    return {
        "backfillable": [
            {"domain": k, "label": v["label"], "note": v["note"]}
            for k, v in backfill.BACKFILLABLE.items()
        ],
        "refused": [
            {"domain": k, "reason": v} for k, v in backfill.REFUSED.items()
        ],
        "default_start": backfill.DEFAULT_START,
        # 清单本身零联网 —— 它是代码里的常量。
        "offline": True,
    }


@router.get("/pipeline/backfill/plan")
def pipeline_backfill_plan(domain: str,
                          start: str = Query(default=""),
                          end: str = Query(default=""),
                          items: List[str] = Query(default=[]),
                          scope: str = Query(default=""),
                          only_gaps: bool = Query(default=False)
                          ) -> Dict[str, Any]:
    """回填计划：已覆盖什么、还缺什么、要发多少次请求。**零联网。**

    `items` 是板块名或股票代码列表。全市场日线（`domain=daily_bar`）必须二给一：
    传 `items`，或传 `scope`（名单范围 all/mf/pool/wl）—— 名单只从**本地库**的
    snapshot payload 里解，后端不会自己去联网取股票列表（`plan()` 那条报错
    是同一个意思，这里只是给了个省事的入口）。

    :param start:     起始日 YYYYMMDD，缺省用 `DEFAULT_START`（20260801）。
    :param only_gaps: 按**缺口**算待办（阶段 5）。无论开关是什么，中间断档的
                      票都会报在响应的 `holes` 里 —— 那是「首末日看着盖住了、
                      里面其实是空的」那批，普通回填对它们是跳过的。
    """
    from app.pipeline import backfill
    # 本函数既能被 FastAPI 调（参数已解析成值），也能像回归测试那样当普通
    # Python 函数直接调 —— 那时 `items` / `scope` 还是 `Query()` 哨兵而不是值。
    # 拿哨兵去迭代会 TypeError、拿它当名单范围会 400，所以先判类型再用。
    codes: List[str] = list(items) if isinstance(items, list) else []
    scope_s = scope if isinstance(scope, str) else ""
    gaps = bool(only_gaps) if isinstance(only_gaps, bool) else False
    if not codes and scope_s:
        try:
            codes = backfill.load_scope(scope_s)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        return backfill.plan(domain,
                             start or backfill.DEFAULT_START,
                             # `end` 的缺省是**最新交易日**，不是 DEFAULT_START
                             # —— 后者是**起始日**，拿它当结束日会让「只填
                             # start」变成一段方向反了的区间（页面上 end 写死
                             # 空串，一直落在这上面，见 plan.md 阶段 5）。
                             end or _latest_trade_date() or
                             backfill.DEFAULT_START,
                             items=codes, only_gaps=gaps)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("回填计划失败 %s: %s", domain, exc, exc_info=True)
        raise HTTPException(
            500, f"回填计划失败: {type(exc).__name__}: {exc}") from exc


@router.post("/pipeline/backfill")
def pipeline_backfill(payload: Dict[str, Any]) -> Dict[str, Any]:
    """执行回填。

    ⚠️ **本端点会联网**，且是**几千次**请求量级 —— 只在人明确点了「开始回填」
    时才调。服务启动、页面刷新、任何定时器都**不会**碰它。

    :body: ``{domain, start, end, items?|scope?, budget?, only_gaps?}``
           `budget` 限定本次最多发多少次取数请求；返回 `finished=false` 表示
           还有活没干完，**原样再调一次即接着跑**（断点续跑不另建进度表）。
           `scope` 与 `items` 同理二给一，名单一律取自本地库。
           `only_gaps=true` = 只补缺口（阶段 5）：逐票比对已有交易日，中间断档
           的段单独重试，一天不缺就一次请求都不发。

    当前内网环境下抓股票接口会被公司安全设备拦截并留记录 —— 见
    `docs/data_pipeline/audit.md` §12。
    """
    from app.pipeline import backfill as bf
    domain = str(payload.get("domain") or "")
    codes = list(payload.get("items") or [])
    if not codes and payload.get("scope"):
        try:
            codes = bf.load_scope(str(payload["scope"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    start = str(payload.get("start") or bf.DEFAULT_START)
    # 结束日缺省 = 最新交易日（同 plan 端点）：DEFAULT_START 是**起始日**。
    end = str(payload.get("end") or _latest_trade_date() or bf.DEFAULT_START)

    # 回填走**统一任务入口**（任务书阶段 3：六个任务只有一个触发口，第二套入口
    # 迟早漂移成「改了任务忘了改按钮」）。job_run 登记、状态词、参数校验都在
    # `tasks.run_task` 里收口，这里只留请求参数的整形（scope → 名单）。
    # `finished=false` 的续跑会各自留一条任务记录 —— 那本来就是一次次独立的活。
    try:
        return tasksvc.run_task("historical_backfill", trade_date=end,
                                domain=domain, start=start, end=end,
                                items=codes,
                                budget=payload.get("budget"),
                                # 只补缺口：透传给任务层（它会原样传给 backfill）
                                only_gaps=bool(payload.get("only_gaps"))
                                )["result"]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("回填失败 %s: %s", domain, exc, exc_info=True)
        raise HTTPException(
            500, f"回填失败: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------ 历史补录


class ReplenishBody(BaseModel):
    job: str = Field(..., description="bars / inflow / pools / score")
    start: Optional[str] = Field(None, description="起始日 YYYYMMDD，缺省 20260101")
    end: Optional[str] = Field(None, description="结束日 YYYYMMDD，缺省最新交易日")


@router.get("/replenish/status")
def replenish_status() -> Dict[str, Any]:
    """历史补录四个 job 的状态。**零联网** —— 只读内存状态字典。"""
    return {"jobs": replenish.snapshot(), "active": replenish.active_job()}


@router.post("/replenish/start")
def replenish_start(body: ReplenishBody) -> Dict[str, Any]:
    """起一个补录 job（后台守护线程），立即返回。

    ⚠️ **本端点会联网**，且是几百到上万次请求量级 —— 只在人点「历史补录」
    页的按钮时调。服务启动、页面刷新、任何定时器都不碰它（同
    `/pipeline/backfill` 一条纪律）。断点续跑：原样再点一次就是续跑。
    """
    try:
        return replenish.run_replenish(body.job, body.start, body.end)
    except replenish.Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/replenish/stop")
def replenish_stop(body: ReplenishBody) -> Dict[str, Any]:
    """请求优雅停止某个补录 job（票/批边界退出，已落库的都保留）。"""
    try:
        return replenish.stop(body.job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ----------------------------------------------------------------- 内部


def _latest_trade_date() -> Optional[str]:
    """优先用库内已有日期（回看历史），否则用**只读**日历推最近交易日。

    日历只走 `cached_trade_dates()`，不走 `last_trade_date()`：后者在缓存过
    30 天时会去新浪拉一次日历，而这个函数会被 `GET /pipeline/backfill/plan`
    这种**零联网**端点调到 —— 页面瞄一眼「能补到哪天」就外发一次，正是
    验收 B3 盯着的那种事（本文件 1137/1561 两处也是同一个口径）。
    """
    for dataset in ("pool_zt", "pool_zb", "pool_dt"):
        d = db.latest_date(dataset)
        if d:
            return d
    try:
        today = datetime.now().strftime("%Y%m%d")
        dates = [d for d in tcal.cached_trade_dates() if d <= today]
        return dates[-1] if dates else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历不可用: %s", exc)
        return None


# ------------------------------------------ 自动买卖策略 V1（docs/ai_trading_lab/）
# 全部零联网：候选 = 本地 `main_force` 快照、行情 = `daily_bar`、日历只用
# `cached_trade_dates()`（在 service 内）、风险状态 = `ingest_job`。联网
# 白名单（DATA_SRC_NET 那 9 条）与此段无关 —— 这里的被调属性名一个都不命中
# `_is_net` 判据，加多少条都只是本地读写。


class TradingRunBody(BaseModel):
    date: Optional[str] = Field(
        None, pattern=r"^\d{8}$",
        description="信号日 YYYYMMDD；缺省取最新六维快照日")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="参数覆盖；未知键/越界值直接 400，不静默忽略")


class TradingExecBody(BaseModel):
    exec_date: Optional[str] = Field(
        None, pattern=r"^\d{8}$",
        description="执行日；缺省取最早待执行日")


@router.post("/trading/signal/run")
def trading_signal_run(body: TradingRunBody = TradingRunBody()
                       ) -> Dict[str, Any]:
    """生成信号 → 风控 → 下单。同日重复调用幂等（UNIQUE 策略+信号日）。"""
    try:
        return tradingsvc.run_signals(body.date, body.params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/trading/execute")
def trading_execute(body: TradingExecBody = TradingExecBody()
                    ) -> Dict[str, Any]:
    """在下一交易日开盘价上撮合待执行订单。幂等；执行日无行情则拒绝开跑
    （ran=false），绝不无价成交。"""
    return tradingsvc.execute(body.exec_date)


@router.get("/trading/status")
def trading_status() -> Dict[str, Any]:
    """账户 / 持仓 / 待执行单 / 最近一次 run（只读本地库）。"""
    return tradingsvc.status()


@router.get("/trading/runs")
def trading_runs(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    return {"items": tradingsvc.runs(limit)}


@router.get("/trading/signals")
def trading_signals(run_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    """某次 run 的全部信号（含 HOLD：每条不交易都写明为什么）。"""
    return {"items": tradingsvc.signals(run_id)}


@router.get("/trading/orders")
def trading_orders(limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
    return {"items": tradingsvc.orders(limit)}


@router.get("/trading/params")
def trading_params() -> Dict[str, Any]:
    """默认参数 + 版本口径（与页面表单同一份事实）。"""
    return tradingsvc.params_info()
