"""对**运行中的站点**做一次只读验收：新接口在不在、前端资产是不是新版。

不启动任何东西、不改任何状态，只打 GET。区别于 smoke 脚本（那些直接构造应用），
这里确认的是"服务真的重启了、用户刷新页面就能用上新功能"。
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8878"
PASS, FAIL = "[OK]  ", "[FAIL]"


def get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f"  {detail}" if detail else ""))


print(f"目标：{BASE}")

status, body = get("/api/health")
check("健康检查 200", status == 200, f"HTTP {status}")
if status == 200:
    health = json.loads(body)
    print(f"  uptime={health.get('uptime_seconds')}s packages="
          f"{ {k: v['ok'] for k, v in (health.get('packages') or {}).items()} }")
    check("服务刚刚重启（uptime < 300s）",
          int(health.get("uptime_seconds") or 0) < 300,
          f"uptime={health.get('uptime_seconds')}s（没重启的话这里会是几万秒）")

status, body = get("/api/openapi.json")
check("接口清单可读", status == 200, f"HTTP {status}")
paths = set(json.loads(body).get("paths", {})) if status == 200 else set()
check("新接口 /api/writing/import-plan 已注册",
      "/api/writing/import-plan" in paths, str(sorted(p for p in paths if "writing" in p)))
check("新接口 /api/writing/section 已注册", "/api/writing/section" in paths)

status, body = get("/api/capabilities")
check("能力清单可读", status == 200, f"HTTP {status}")
if status == 200:
    writing = next((a for a in json.loads(body)["assistants"] if a["id"] == "writing"), {})
    check("能力清单已写到新版文案（提到导入 Word/PDF）",
          "导入" in writing.get("summary", ""), writing.get("summary", "")[:80])

status, body = get("/api/agents/writer/actions")
check("writer agent 动作清单可读", status == 200, f"HTTP {status}")
if status == 200:
    names = {a["name"] for a in json.loads(body).get("actions", [])}
    check("agent 侧也注册了 import_plan / section",
          {"import_plan", "section"} <= names, str(sorted(names)))

status, page = get("/")
check("首页可访问", status == 200, f"HTTP {status}")
check("首页含新的导入拖放区（wDrop）", 'id="wDrop"' in page)
check("首页含写作要求输入框（wRequirements）", 'id="wRequirements"' in page)
check("首页写的是四层依据", "D 你导入的文档" in page)

status, js = get("/static/app.js")
check("前端脚本可访问", status == 200, f"HTTP {status}")
check("前端含单章重写（runSectionRewrite）", "runSectionRewrite" in js)
check("前端含导入计划接口调用", "/api/writing/import-plan" in js)

status, css = get("/static/theme.css")
check("样式表含单章重写样式（.rewrite-box）", status == 200 and ".rewrite-box" in css)

print()
failed = [c for c in checks if not c[1]]
if failed:
    print(f"失败 {len(failed)} 项：")
    for name, _, detail in failed:
        print(f"  - {name}  {detail}")
    raise SystemExit(1)
print(f"全部通过（{len(checks)} 项）——线上（本机常驻服务）已是新版")
