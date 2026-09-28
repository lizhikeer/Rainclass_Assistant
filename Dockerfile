# 飞牛 NAS / Docker 部署 Dockerfile
ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

# 统一时区为 Asia/Shanghai，纯后台无头模式，固定 Chromium 路径，禁止 Python 缓冲
ENV TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 优化 apt 源为国内清华镜像，安装基础依赖与中文字体支持
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true; \
    sed -i 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true; \
    sed -i 's|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装纯后台 Worker 依赖（已剥离桌面 Tkinter 相关依赖）
COPY requirements-worker.txt /app/requirements-worker.txt
RUN pip install --no-cache-dir -r requirements-worker.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装运行 Chromium 所需的 apt 系统共享库依赖（严格安装，失败直接终止构建）
RUN python -m playwright install-deps chromium

# 固化已准备的 Chromium 与 FFmpeg 二进制包（若本地存在 ms-playwright 优先复用，否则构建阶段自动下载）
COPY ms-playwright* /ms-playwright/
RUN if [ ! -d /ms-playwright/chromium* ]; then python -m playwright install chromium; fi

# 复制应用核心源码
COPY src/ /app/src/

# 创建持久化数据目录并保障非 root 用户权限（对齐飞牛 NAS 默认用户 uid=1000, gid=1001）
RUN groupadd -g 1001 rainclass 2>/dev/null || true && \
    useradd -u 1000 -g 1001 -m -s /bin/bash rainclass 2>/dev/null || true && \
    mkdir -p /app/data && \
    chown -R 1000:1001 /app /ms-playwright 2>/dev/null || true

USER 1000:1001

ENTRYPOINT ["python", "-m", "src.worker"]
CMD ["--data-dir", "/app/data", "--wait-for-session"]
