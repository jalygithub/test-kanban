# -*- coding: utf-8 -*-
r"""「数据管理」页阶段 6 改动 + 「自动交易」页（模拟盘）的**源码级**回归，
2026-10-17 / 2026-10-19（60 条）。

前端没有测试运行器（package.json 只有 tsc / oxlint / vite build），所以这里
用源码断言盯住几条**行为级**口径 —— 它们错了，tsc 与 oxlint 一个都不会红：

  1) **首屏只读。** 页面挂载只走 `loadPipe`，而 `loadPipe` 只调六个
     **GET、零联网** 的只读端点（覆盖 / 漏采 / 采集状态 / 行情进度 /
     健康体检 / 任务进度）。会外发请求的三个动作（补跑重试、开始回填、
     跑这一轮）**只在人的 onClick / onConfirm 里** —— 内网下的一次误外发
     会被安全设备拦截并记录（任务书阶段 6 硬约束，A/B 段）。
  2) **调度在本页只读。** 开/关调度的写接口 `setSchedule` 不许出现在
     DataManage（那属于「定时任务」页的按钮）（B 段）。
  3) **缺失区间与任务进度收敛到一份实现。** `fmtRanges()`（纯函数、渲染期
     不碰 Date）与 `JobProgress` 组件在 format.ts / components/ 各只有一份，
     两页各写一遍的话口径迟早分叉（D/E 段）。
  4) **自动交易页的边界。** 首屏四个 GET 全读本地库；会推进状态的两个 POST
     （生成信号、执行订单）只在人的点击里，页面无轮询；「不实盘、不发真实
     下单指令」与免责声明必须原文在页（T 段）。

纯读文本、零 import 前端代码，也零网络（脚本本身不做 IO 之外的任何事）。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_ui_offline_check.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend" / "src"
DM = (FE / "pages" / "DataManage.tsx").read_text(encoding="utf-8")
OV = (FE / "pages" / "Overview.tsx").read_text(encoding="utf-8")
API = (FE / "lib" / "api.ts").read_text(encoding="utf-8")
FMT = (FE / "lib" / "format.ts").read_text(encoding="utf-8")
JP = (FE / "components" / "JobProgress.tsx").read_text(encoding="utf-8")
ROUTES = (ROOT / "backend" / "app" / "api" / "routes.py").read_text(
    encoding="utf-8")

PASS = 0
FAILS = []


def ok(cond, msg):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(msg)


def _between(text, start, end):  # noqa: ANN001
    """start..end 之间的一段源码；找不到端点就返回到文件尾（断言会红）。"""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i:j] if j > 0 else text[i:]


def _line(text, anchor):  # noqa: ANN001
    """含 anchor 的那一行（单行声明用它，别跨行切片）。"""
    i = text.find(anchor)
    if i < 0:
        return ""
    a, b = text.rfind("\n", 0, i) + 1, text.find("\n", i)
    return text[a:b if b > 0 else len(text)]


def _window(text, anchor, size=600):  # noqa: ANN001
    """以 anchor 起点的一小段（默认 600 字符）—— 列定义这类就看这一块。"""
    i = text.find(anchor)
    return text[i:i + size] if i >= 0 else ""


# ---- A) 首屏加载路径：六个只读端点 ------------------------------------------
load = _between(DM, "const loadPipe", "useEffect(() => {")
GETTERS = {
    "api.pipelineCoverage()": "/api/pipeline/coverage",
    "api.pipelineGaps(30)": "/api/pipeline/gaps",
    "api.pipelineIngest(50)": "/api/pipeline/ingest",
    "api.pipelineBars('all', 5)": "/api/pipeline/bars",
    "api.pipelineHealth()": "/api/pipeline/health",
    "api.jobStatus()": "/api/jobs/status",
}
ok("Promise.allSettled" in load, "A0 loadPipe 必须用 allSettled（一挂全挂不行）")
for _i, (_call, _url) in enumerate(sorted(GETTERS.items())):
    ok(load.count(_call) == 1, "A%d loadPipe 里 %s 恰好 1 次（实际 %d）"
       % (_i + 1, _call, load.count(_call)))
    ok(_url in API, "A%d api.ts 里 %s -> %s（零联网 GET）"
       % (_i + 7, _call, _url))
ok("useEffect(() => {\n    void loadPipe()\n  }, [loadPipe])" in DM,
   "A13 挂载 effect 只调 loadPipe、依赖只有它")

# ---- B) 三个联网动作只在人的点击里 ------------------------------------------
retry_body = _between(DM, "const doRetry", "const runDatedJob")
back_body = _between(DM, "const doBackfill", "coverageColumns")
job_body = _between(DM, "const runDatedJob", "const doBackfill")
ok(retry_body.count(".collectRetry(") == 1 and
   ".collectRetry(" not in DM.replace(retry_body, "", 1),
   "B1 collectRetry 只在 doRetry 里")
ok(back_body.count(".backfillRun(") == 1 and
   ".backfillRun(" not in DM.replace(back_body, "", 1),
   "B2 backfillRun 只在 doBackfill 里")
ok(job_body.count(".runJob(") == 1 and
   ".runJob(" not in DM.replace(job_body, "", 1),
   "B3 runJob 只在 runDatedJob 里")
ok("onClick={doRetry}" in DM, "B4 补跑按钮 onClick=doRetry")
ok("onClick={runDatedJob}" in DM, "B5 跑这一轮按钮 onClick=runDatedJob")
ok("onConfirm={doBackfill}" in DM, "B6 回填走 Popconfirm 二次确认")
ok("setSchedule" not in DM, "B7 调度开关在本页**只读**（写接口不出现）")
ok(all(x not in load for x in (".collectRetry", ".backfillRun", ".runJob",
                               "setSchedule")),
   "B8 加载路径里没有任何会外发的动作")

# ---- C) 只读端点确实是 GET ---------------------------------------------------
ok("method" not in _line(API, "pipelineHealth:"),
   "C1 pipelineHealth 没带 method -> GET")
ok("method" not in _line(API, "jobStatus:"),
   "C2 jobStatus 没带 method -> GET")
ok('@router.get("/pipeline/health")' in ROUTES,
   "C3 后端 /pipeline/health 是 GET 装饰器")

# ---- D) 缺失区间合并：一份纯函数 --------------------------------------------
fbody = FMT[FMT.find("export function fmtRanges"):] if \
    "export function fmtRanges" in FMT else ""
nextday = _between(FMT, "const nextDay", "export function fmtRanges")
ok("export function fmtRanges" in FMT, "D1 fmtRanges 定义且导出")
ok("nextDay" in fbody and ".sort()" in fbody,
   "D2 fmtRanges 先排序再按 nextDay 合并（顺序不靠调用方）")
ok("new Date" not in nextday and "new Date(" not in fbody,
   "D3 渲染期路径不碰 new Date（React Compiler 记忆化）")
ok(DM.count("fmtRanges(") >= 4,
   "D4 域级 + 个股级两处都用 fmtRanges（实际 %d 处）"
   % DM.count("fmtRanges("))
ok("fmtRanges(v)" in _window(DM, "title: '缺的日期'"),
   "D5 个股级缺日改成区间显示（不再是逗号串）")

# ---- E) 任务进度：共享组件 ---------------------------------------------------
ok("export function JobProgress" in JP, "E1 components/JobProgress 导出")
for _name, _src in (("总览", OV), ("数据管理", DM)):
    ok("components/JobProgress" in _src, "E2 %s 页 import 共享 JobProgress"
       % _name)
ok("function JobProgress" not in OV, "E3 总览页不再自带一份实现")
ok("STEP_LABEL" not in DM, "E4 数据管理页不另抄步骤名映射")

# ---- F) 汇总卡零新请求 ------------------------------------------------------
card = _between(DM, "{/* 阶段 6 汇总卡", 'title="采集覆盖与缺口"')
ok('title="数据质量总览"' in card, "F1 汇总卡在位")
ok("api." not in card, "F2 汇总卡零新请求（数字全部复用已取的 state）")
ok("health?.last_success" in card and "jstat.schedule" in card,
   "F3 健康体检与调度状态接上了")

# ---- T) 自动交易页（模拟盘）：首屏只读 + 动作只在点击里 ----------------------
TR = (FE / "pages" / "Trading.tsx").read_text(encoding="utf-8")
tload = _between(TR, "const load = useCallback", "useEffect(() => {")
TGETTERS = {
    "api.tradingStatus()": "/api/trading/status",
    "api.tradingRuns(20)": "/api/trading/runs",
    "api.tradingOrders(100)": "/api/trading/orders",
    "api.tradingParams()": "/api/trading/params",
}
ok("Promise.allSettled" in tload, "T1 首屏用 allSettled（一挂全挂不行）")
for _i, (_c, _u) in enumerate(sorted(TGETTERS.items())):
    ok(tload.count(_c) == 1, "T%d 首屏 %s 恰好 1 次（实际 %d）"
       % (_i + 2, _c, tload.count(_c)))
    ok(_u in API, "T%d api.ts 有 %s（零联网 GET）" % (_i + 6, _c))
ok("useEffect(() => {\n    void load()\n  }, [load])" in TR,
   "T10 挂载 effect 只调 load、依赖只有它")
trun_body = _between(TR, "const runSignals", "const execOrders")
texe_body = _between(TR, "const execOrders", "const positionColumns")
ok(trun_body.count(".tradingSignalRun(") == 1 and
   ".tradingSignalRun(" not in TR.replace(trun_body, "", 1),
   "T11 生成信号只在 runSignals 里")
ok(texe_body.count(".tradingExecute(") == 1 and
   ".tradingExecute(" not in TR.replace(texe_body, "", 1),
   "T12 执行订单只在 execOrders 里")
ok("onClick={runSignals}" in TR, "T13 生成信号按钮接 onClick")
ok("onConfirm={execOrders}" in TR, "T14 执行订单走 Popconfirm 二次确认")
ok(all(x not in tload for x in (".tradingSignalRun", ".tradingExecute")),
   "T15 首屏零动作")
ok("setInterval" not in TR and "setTimeout" not in TR,
   "T16 页面无轮询（推进全靠人点）")
ok("method" not in _line(API, "tradingStatus:") and
   "method" not in _line(API, "tradingRuns:") and
   "method" not in _line(API, "tradingSignals:") and
   "method" not in _line(API, "tradingOrders:") and
   "method" not in _line(API, "tradingParams:"),
   "T17 五个读端点都是 GET（不带 method）")
ok("method: 'POST'" in _between(API, "tradingSignalRun:",
                                "tradingStatus:") and
   "method: 'POST'" in _between(API, "tradingExecute:", "tradingStatus:"),
   "T18 仅有的两个 POST 显式标出")
ok('@router.get("/trading/status")' in ROUTES and
   '@router.get("/trading/runs")' in ROUTES and
   '@router.get("/trading/signals")' in ROUTES and
   '@router.get("/trading/orders")' in ROUTES and
   '@router.get("/trading/params")' in ROUTES,
   "T19 后端五个读路由是 GET 装饰器")
ok('@router.post("/trading/signal/run")' in ROUTES and
   '@router.post("/trading/execute")' in ROUTES,
   "T20 后端两个动作路由是 POST 装饰器")
ok("模拟盘" in TR and "免责声明" in TR,
   "T21 模拟盘口径与免责声明都在页面上")
ok("不接实盘" in TR and "不发任何真实下单" in TR,
   "T22 「不实盘、不发真实下单」原文写在页面上")

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
