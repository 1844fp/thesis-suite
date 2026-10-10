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


def _log_config(log_file: str) -> dict:
    """把 uvicorn 的日志改写到文件。

    为什么要让 **Python 自己**写日志、而不是由启动脚本重定向输出：
    常驻服务是被 ``Start-Process`` 拉起来的，而 ``Start-Process`` 一旦带上
    ``-RedirectStandardOutput``，.NET 会等子进程退出才返回（``UseShellExecute=false``
    的行为）——常驻服务永远不退出，于是"启动"命令永远不返回。实测踩过两次：
    服务明明起来了，命令却挂到超时被杀，还把服务一起带走。

    带 ``-PassThru`` 也一样会持有输出句柄。结论是**一个管道都不能用**，
    所以日志必须在 Python 这一侧落到文件。

    顺便这个方案还解决了编码问题：PowerShell 5.1 从管道读文本时按 ANSI 解码，
    会把 Python 的 UTF-8 中文日志写成乱码。
    """
    from uvicorn.config import LOGGING_CONFIG

    import copy

    config = copy.deepcopy(LOGGING_CONFIG)
    config["handlers"]["default"] = {
        "class": "logging.FileHandler",
        "filename": log_file,
        "encoding": "utf-8",
        "formatter": "default",
    }
    # access 日志（每个请求一行）在常驻场景下会迅速把文件撑大，且对"能不能打开"
    # 没有帮助，所以只留 error 级别。
    config["handlers"]["access"] = {
        "class": "logging.FileHandler",
        "filename": log_file,
        "encoding": "utf-8",
        "formatter": "access",
    }
    config["loggers"]["uvicorn.access"]["level"] = "WARNING"
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suite", description="论文助手 · 四合一站点")
    parser.add_argument("--host", default=os.environ.get("SUITE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUITE_PORT", "8848")))
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--log-file", default=os.environ.get("SUITE_LOG_FILE", ""),
                        help="把运行日志写到该文件（常驻服务用；不填则输出到控制台）")
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

    log_config = None
    if args.log_file:
        from pathlib import Path

        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        log_config = _log_config(args.log_file)

    print("=" * 64)
    print("  论文助手 · 四合一（题目生成 / 资料获取 / 写作 / 评估）")
    print("=" * 64)
    print(f"  界面地址 : http://{args.host}:{args.port}/")
    print(f"  接口文档 : http://{args.host}:{args.port}/api/docs")
    if args.log_file:
        print(f"  运行日志 : {args.log_file}")
    print("  提示     : 访客自带 API Key，服务器不保存任何密钥")
    print("=" * 64, flush=True)

    # reload=False：本项目的模块树依赖 sys.path 引导，热重载会重复执行引导逻辑。
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info",
                log_config=log_config, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
