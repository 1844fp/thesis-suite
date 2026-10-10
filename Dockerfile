# 论文助手 · 四合一站点
#
# 部署目标：Hugging Face Spaces（Docker SDK）。
# 这个镜像里**不含任何模型密钥**——站点是 BYOK 的，密钥由访客在浏览器里填、
# 随请求头转发给模型服务商。所以这个 Space 不需要配置任何 secret 就能跑。

FROM python:3.13-slim

#   PYTHONDONTWRITEBYTECODE  容器里不需要 .pyc，省镜像层
#   PYTHONUNBUFFERED         日志要能实时出现在 Space 的日志面板里
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    SUITE_HOST=0.0.0.0 \
    SUITE_PORT=7860 \
    DSH_THESIS_DATA_DIR=/app/data

WORKDIR /app

# 先装依赖：源码改动不会让这一层缓存失效。
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# 站点自己的代码 + 两套原样搬运的上游代码 + 前端
COPY suite /app/suite
COPY vendor /app/vendor
COPY web /app/web

# Hugging Face Spaces 要求以非 root 用户运行。
# 数据目录只用于导出与缓存，容器里本来就是临时的，不挂卷。
RUN useradd -m -u 1000 appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app

USER appuser

EXPOSE 7860

# Spaces 会轮询这个端口判断容器是否就绪；healthcheck 失败时平台会重启容器。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4).status==200 else 1)"

# 用 --factory：应用工厂里会先执行 sys.path 引导（suite.boot.prepare），
# 直接给 uvicorn 传字符串导入路径才能保证引导先于上游模块导入发生。
CMD ["python", "-m", "uvicorn", "suite.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "7860"]
