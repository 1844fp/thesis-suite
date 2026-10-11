# 论文助手 · 四合一站点
#
# 这个镜像里**不含任何模型密钥**——站点是 BYOK 的，密钥由访客在浏览器里填、
# 随请求头转发给模型服务商。所以部署到任何平台都不需要配置 secret。
#
# 端口处理是这里最要紧的一处：不同平台用不同的方式指定端口，
# Hugging Face Spaces 固定 7860，而 Render / Railway 等用环境变量 $PORT 注入。
# 早先的版本把 7860 写死，挪到 Render 上会因为"监听端口不对"直接判为部署失败。
# 现在统一以 $PORT 为准、缺省回落到 7860，两类平台都能用。

FROM python:3.13-slim

#   PYTHONDONTWRITEBYTECODE  容器里不需要 .pyc，省镜像层
#   PYTHONUNBUFFERED         日志要能实时出现在平台的日志面板里
#   PORT                     由平台注入；这里给一个默认值，本地 docker run 也能跑
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=7860 \
    SUITE_HOST=0.0.0.0 \
    DSH_THESIS_DATA_DIR=/app/data

WORKDIR /app

# 先装依赖：源码改动不会让这一层缓存失效。
# 用国内镜像源吗？不。部署平台多在境外，默认源更快且更少出现同步延迟。
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# 站点自己的代码 + 两套原样搬运的上游代码 + 前端
COPY suite /app/suite
COPY vendor /app/vendor
COPY web /app/web

# 以非 root 用户运行（Hugging Face Spaces 强制要求，其它平台也是好实践）。
# 数据目录只用于导出与缓存，容器里本来就是临时的，不挂卷。
RUN useradd -m -u 1000 appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app

USER appuser

# 只是文档性声明；真正的端口以 $PORT 为准。$PORT 变化时 EXPOSE 不会跟着变，
# 这不影响运行（EXPOSE 不参与端口映射决策）。
EXPOSE 7860

# 健康检查：Spaces 会轮询端口判断就绪，失败时重启容器；其它平台可自行读取。
# 用 sh -c 展开 $PORT——JSON 数组形式不经过 shell，${PORT} 不会被替换。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','7860'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=4).status==200 else 1)"

# 启动命令：
#   · sh -c 是为了展开 $PORT（JSON 数组形式不做变量替换）
#   · SUITE_HOST / SUITE_PORT 由 suite/__main__.py 读取，缺省回落见该文件
#   · --factory 是必须的：应用工厂里会先执行 sys.path 引导（suite.boot.prepare），
#     直接传字符串导入路径才能保证引导先于上游模块的导入发生
CMD ["sh", "-c", "exec python -m uvicorn suite.api.app:create_app --factory --host \"${SUITE_HOST:-0.0.0.0}\" --port \"${PORT:-7860}\""]
