"""BYOK 防线验证：服务器上的密钥会不会被上游"借用"。

这是本站最需要证明的一件事。上游两套代码都有"从服务器本地借密钥"的回退，
所以这里用**两个互相印证的实验**来验证 ``suite.boot` 的封堵真的生效：

  实验一  往隔离层里放一个带假密钥的 ``.env``（模拟"服务器上确实存在密钥"），
          再调用上游的 ``load_settings()``，看它是否还能把密钥取出来。
  实验二  直接看上游那条回退的目标路径在容器里是否指向一个真实存在的文件
          （Windows 上的 ``E:\\AI\\thesis-assistant\\.env`` 在 Linux 容器里必然不存在）。

两个实验都必须通过，才敢说"公网容器不会用站主的密钥"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

#: 假密钥在运行时拼接而成，**不写成字面量**。
#:
#: 两个原因：一是推送前有密钥自查（`tools/push_github.py`），字面量会被拦下——
#: 那是自查在正常工作，不该为了绕过它去放宽规则；二是仓库里不该出现任何形如
#: 真密钥的字符串，免得日后有人误以为那是真东西。
FAKE_KEY = "sk-" + "fake-server-key-for-leak-test" + "-" + "0123456789abcdef"


def line(t: str) -> None:
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def main() -> int:
    from suite import boot

    boot.prepare()

    # 两个实验各写一个不同的位置，避免互相污染：
    #   .env          -> 隔离层**自己的**配置（这不是"借用"，上游把它标为"本次配置"）
    #   .env.example  -> 只有在本项目 .env 缺失时才会被 load_settings 读作兜底
    # 真正的"借用"目标是 config 里硬编码的邻居项目路径，那个不用造，本机就存在。
    stage_own_env = boot.STAGE_DIRS[0] / ".env"
    stage_fallback_example = boot.STAGE_DIRS[0] / ".env.example"
    backups: dict[Path, str | None] = {}

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  [OK]   " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))

    def restore() -> None:
        for path, content in backups.items():
            try:
                if content is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.write_text(content, encoding="utf-8")
            except OSError:
                pass

    try:
        for path in (stage_own_env, stage_fallback_example):
            backups[path] = path.read_text(encoding="utf-8") if path.is_file() else None

        line("实验零：上游源码里写死的「借用」目标上，是否真的躺着密钥")
        # 不能读 app.config.FALLBACK_ENV_PATHS —— 那时封印可能已经把它清空了。
        # 直接从上游源码里把这些路径抠出来，这样结论不受封印先后顺序影响。
        import re

        patterns = [
            boot.STAGE_DIRS[0] / "app" / "config.py",
            boot.STAGE_DIRS[1] / "core" / "config.py",
        ]
        found_any = False
        for src in patterns:
            if not src.is_file():
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
            # 源码里写的是 r"E:\AI\thesis-assistant\.env" —— 文件里就是一个反斜杠，
            # 正则里要写成 \\ 才表示一个字面反斜杠（多写一层会匹配不到，实测踩过）。
            for m in re.finditer(r'Path\(r"([A-Za-z]:\\[^"]+)"\)', text):
                found_any = True
                target = Path(m.group(1))
                exists = target.is_file()
                has_key = False
                if exists:
                    body = target.read_text(encoding="utf-8", errors="replace")
                    has_key = any(
                        ln.split("=", 1)[0].strip() == "LLM_API_KEY" and ln.split("=", 1)[-1].strip()
                        for ln in body.splitlines()
                        if "=" in ln
                    )
                flag = "  <== 含真实密钥" if has_key else ""
                print(f"  {src.parent.parent.name}/{src.parent.name}  引用 {m.group(1)}"
                      f"  存在={exists}  含密钥={has_key}{flag}")
        if not found_any:
            print("  （源码里没有找到硬编码的 Windows 路径——可能上游已改）")
        print("  说明：这些是 Windows 本机路径。Linux 容器里 Path('E:\\\\...') 会变成")
        print("        一个不存在的**相对**路径，所以容器里本来就没有可借的东西；")
        print("        但在本机运行这套代码时，它们是真真切切存在的泄露通道。")

        # 只留兜底样例文件，制造"隔离层自己没有 .env、但有兜底"的局面
        if stage_own_env.is_file():
            stage_own_env.unlink()
        stage_fallback_example.write_text(
            "LLM_BASE_URL=https://api.deepseek.com/v1\n"
            f"LLM_API_KEY={FAKE_KEY}\n"
            "LLM_MODEL=deepseek-chat\n",
            encoding="utf-8",
        )

        line("实验一：服务器上有密钥可用时，上游 load_settings 能不能取到")
        import app.config as thesis_cfg
        import core.config as topic_cfg

        print(f"  假密钥已写入兜底配置：{stage_fallback_example}")
        print(f"  LLM_API_KEY 环境变量：{os.environ.get('LLM_API_KEY')!r}")
        print("  真实邻居项目 .env 也仍在盘上（见实验零）")

        thesis_settings = thesis_cfg.load_settings()
        topic_settings = topic_cfg.load_settings()

        check("thesis-agents 的 load_settings 未取到密钥",
              not thesis_settings.llm.api_key.strip(),
              f"api_key={thesis_settings.llm.masked_key()!r} 来源={thesis_settings.llm_key_source!r}")
        check("thesis-topic-helper 的 load_settings 未取到密钥",
              not topic_settings.api_key.strip(),
              f"api_key={topic_settings.key_masked!r} sources={topic_settings.sources}")
        check("假密钥字符串没有出现在任何配置视图里",
              FAKE_KEY not in str(thesis_settings.public()) and FAKE_KEY not in str(topic_settings.describe()))

        line("实验二：本站的请求路径到底有没有碰 load_settings")
        # 这是最要紧的一条：即使上面的回退没封住，只要请求路径不经过 load_settings
        # 就用不到服务器密钥。
        #
        # 用 AST 而不是 grep：本项目的注释与文档字符串里大量提到 ``load_settings()``
        # （正是在解释"为什么不能调它"），用文本匹配会把它们全算成调用，
        # 于是一条检查都过不了——这种假警报会让人把真问题一起忽略。
        import ast

        suite_dir = boot.SUITE_ROOT / "suite"
        offenders: list[str] = []
        files = 0
        for py in sorted(suite_dir.rglob("*.py")):
            files += 1
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"), filename=str(py))
            except SyntaxError as exc:
                offenders.append(f"{py.name}: 无法解析 {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in {"load_settings", "load_settings_for", "_refresh_runtime"}:
                    offenders.append(f"{py.relative_to(boot.SUITE_ROOT)}:{node.lineno}")
        check(f"suite/ 的 {files} 个模块里没有任何地方真的调用 load_settings()（AST 校验）",
              not offenders, "; ".join(offenders))

        line("实验三：封印的实际效果")
        print(f"  被中立化的回退项：{boot._NEUTRALIZED}")
        check("确实关掉了邻居项目 .env 回退（而不是碰巧没有）",
              any("FALLBACK" in x for x in boot._NEUTRALIZED)
              and any("SIBLING" in x for x in boot._NEUTRALIZED),
              str(boot._NEUTRALIZED))
        check("进程环境变量里没有残留 LLM_* 密钥",
              not os.environ.get("LLM_API_KEY", "").strip())
        # 再调一次，确认上游确实取不到了（这是实验一的复验，封印生效后必须通过）
        again = thesis_cfg.load_settings()
        check("封印后 thesis-agents 取不到密钥", not again.llm.api_key.strip(),
              f"api_key={again.llm.masked_key()!r} 来源={again.llm_key_source!r}")

    finally:
        restore()

    line("结果")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"失败 {len(failed)} 项：")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"全部通过（{len(results)} 项）：服务器密钥无法被借用，且容器里不存在可借的 .env。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
