# 通用部署镜像 —— Hugging Face Spaces / Render / Railway / Fly.io 都能直接用
FROM python:3.12-slim

# 不写 .pyc、日志不缓冲（否则平台日志面板看不到实时输出）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖再拷代码：改代码时这一层的构建缓存不会失效
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite 放到 /tmp —— 多数平台的应用目录只读或不持久。
# 预置 Skill 每次启动都会重新灌入，所以库是临时的也不影响 Demo。
ENV SKILL_DB_PATH=/tmp/skills.db

# 平台通过 $PORT 指定端口；HF Spaces 固定 7860，其余平台动态注入
ENV PORT=7860
EXPOSE 7860

# 用 shell 形式让 $PORT 能被展开
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}
