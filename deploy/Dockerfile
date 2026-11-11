# A股看板 · 后端镜像
#
# 构建（必须在仓库根目录执行，.dockerignore 才会生效）：
#   docker build -f deploy/Dockerfile -t kanban-backend:1.0 .
#
# 两条设计取舍：
#   1) 镜像里只有后端。前端 dist 由部署包目录以卷挂载给 nginx —— 前端改版只要
#      重新 npm run build 并覆盖 dist/ 就行，不用重建镜像，也省掉 node 构建阶段。
#   2) backend/data（SQLite，内含飞书 app_secret）与 backend/config（评分模型）
#      刻意**不打进镜像**，运行时挂载：镜像可安全传递给别人、升级镜像不丢数据。
FROM python:3.12-slim

# TZ 必须是 Asia/Shanghai：scheduler.py 给 16:30 的 CronTrigger **不指定时区**，
# 交易日判断又用 date.today()，两者都读系统本地时区。容器默认 UTC 的话，任务会
# 在北京时间 00:30 才跑、交易日标签全错 —— 而且不报任何错，只是数据悄悄不对。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# tzdata 装上再软链，保证 /etc/localtime 指向 CST；curl 留着做容器内自检。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/requirements.txt ./
# 国内构建走清华镜像源；海外机器删掉 -i ... 即可。
# 注意 requirements 只写了 >= 下限，每次构建取当时最新版；
# 想锁版本就本地 pip freeze 出一份 requirements.lock 再 COPY 进来。
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 只拷代码：data 与 config 由 compose 以卷挂载（见 deploy/docker-compose.yml）
COPY backend/app ./app
COPY backend/config ./config
COPY backend/scripts ./scripts

EXPOSE 8000

# 单 worker 是硬约束：多 worker 会让 SQLite 并发写冲突，且 16:30 的 cron 会触发 N 次。
# --no-access-log：请求访问日志交给 nginx，避免淹没应用日志（抓取进度、飞书收发都在应用日志里）。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
