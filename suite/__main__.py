"""统一站点入口。

用法::

    python -m suite              # 本机启动（默认 127.0.0.1:8848）
    python -m suite --host 0.0.0.0 --port 7860    # 容器里监听

生产部署请直接用 uvicorn::

    uvicorn suite.api.app:create_app --factory --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suite", description="论文助手 · 四合一站点")
    parser.add_argument("--host", default=os.environ.get("SUITE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUITE_PORT", "8848")))
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn。本机开发请用 run.ps1（它会带上 vendored 依赖路径）。", file=sys.stderr)
        return 2

    from .api.app import create_app

    if not args.no_browser and args.host in {"127.0.0.1", "localhost"}:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()

    print("=" * 64)
    print("  论文助手 · 四合一（题目生成 / 资料获取 / 写作 / 评估）")
    print("=" * 64)
    print(f"  界面地址 : http://{args.host}:{args.port}/")
    print(f"  接口文档 : http://{args.host}:{args.port}/api/docs")
    print("  提示     : 访客自带 API Key，服务器不保存任何密钥")
    print("=" * 64, flush=True)

    # reload=False：本项目的模块树依赖 sys.path 引导，热重载会重复执行引导逻辑。
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info", reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
