# -*- coding: utf-8 -*-
"""上线前预检：加载站点应用工厂，确认可启动，并列出关键接口与前端资源。

只读：不修改任何文件、不推送、不启动常驻服务。
用法：
    python tools/preflight_123.py                 # 默认检查 E:\\AI\\thesis-suite
    python tools/preflight_123.py --root <dir>
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from pathlib import Path


def load_app(root: Path):
    """用部署文档规定的方式加载应用工厂：模块导入 + --factory，
    **不能**按文件路径加载，否则 app.py 顶部的相对导入（from .. import boot）会失败。

    依赖是 vendored 的（见 run.ps1）。必须让 vendored libs 排在 site-packages 之前，
    否则会用 D:\\py 里过旧的 typing_extensions（缺 sentinel），导入 anyio 直接失败。
    """
    for lib in (str(root / "vendor" / "thesisapp_stage" / "libs"),
                r"E:\AI\aigc-reducer\libs",
                r"E:\AI\thesis-assistant\libs"):
        p = Path(lib)
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import importlib

    mod = importlib.import_module("suite.api.app")
    factory = getattr(mod, "create_app", None)
    if factory is None:
        raise RuntimeError("suite/api/app.py 里没有 create_app")
    return factory()


def smoke_requests(app) -> bool:
    """真正打几个只读请求。子 agent 改坏前端/接口时，这里会先亮红灯。"""
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:  # pragma: no cover
        print(f"  TestClient 不可用（{exc}），跳过请求级冒烟")
        return True

    checks = [
        ("GET", "/", 200),
        ("GET", "/api/health", 200),
        ("GET", "/api/capabilities", 200),
        ("GET", "/api/deployment", 200),
    ]
    ok = True
    print("\n== 请求级冒烟 ==")
    with TestClient(app) as client:
        for method, path, want in checks:
            try:
                resp = client.request(method, path)
            except Exception as exc:
                ok = False
                print(f"  [异常] {method} {path} -> {type(exc).__name__}: {exc}")
                continue
            good = resp.status_code == want
            if not good:
                ok = False
            body = resp.content
            print(f"  [{'OK ' if good else '失败'}] {method} {path:<22} "
                  f"-> {resp.status_code} ({len(body) / 1024:.1f} KB)")
        # 首页必须真的引用到前端资源，否则是空壳
        try:
            html = client.get("/").text
            for asset in ("app.js", "theme.css"):
                if asset not in html:
                    ok = False
                    print(f"  !! 首页没有引用 {asset}")
        except Exception as exc:
            ok = False
            print(f"  !! 首页读取失败：{exc}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"E:\AI\thesis-suite")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    print(f"== 预检根目录 ==\n  {root}")
    ok = True

    # 1) 必需文件
    required = [
        "suite/api/app.py", "suite/__main__.py", "suite/boot.py",
        "web/index.html", "web/app.js", "web/theme.css",
        "requirements.txt", "Dockerfile", "README.md", "run.cmd",
    ]
    print("\n== 必需文件 ==")
    for rel in required:
        p = root / rel
        flag = "OK " if p.is_file() else "缺失"
        if not p.is_file():
            ok = False
        size = f"{p.stat().st_size / 1024:.1f} KB" if p.is_file() else "-"
        print(f"  [{flag}] {rel:<28} {size}")

    # 2) 应用是否可加载
    print("\n== 应用加载 ==")
    try:
        app = load_app(root)
    except Exception:
        ok = False
        print("  加载失败：")
        traceback.print_exc()
        app = None

    if app is not None:
        routes = sorted(
            (getattr(r, "path", "?"), ",".join(sorted(getattr(r, "methods", []) or [])))
            for r in app.routes
        )
        print(f"  加载成功，注册路由 {len(routes)} 条")
        for path, methods in routes:
            print(f"    {methods:<20} {path}")
        if len(routes) < 10:
            ok = False
            print("  !! 路由数偏少，可能引导流程没跑完")
        else:
            ok = smoke_requests(app) and ok

    # 3) 前端资源引用是否自洽
    print("\n== 前端资源引用 ==")
    index = (root / "web" / "index.html")
    if index.is_file():
        html = index.read_text(encoding="utf-8", errors="replace")
        for asset in ("app.js", "theme.css"):
            hit = asset in html
            print(f"  [{'OK ' if hit else '警告'}] index.html 引用 {asset}")
        print(f"  index.html 体积 {len(html) / 1024:.1f} KB")
    js = root / "web" / "app.js"
    if js.is_file():
        src = js.read_text(encoding="utf-8", errors="replace")
        print(f"  app.js 体积 {len(src) / 1024:.1f} KB，行数 {src.count(chr(10)) + 1}")

    print(f"\n== 结论 ==\n  {'预检通过' if ok else '预检未通过，见上方标记'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
