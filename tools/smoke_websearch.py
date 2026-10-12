"""验证互联网文献检索层：四个真实源能否返回可用的记录。

只验证"检索与解析"，不调用任何大模型（不花钱）。
重点看两件事：

1. **能不能返回 `usable` 的记录**（有题名 + 足够长的真实摘要）。
   只有题名的记录对这个功能没用——模型看不到内容就只能猜，那正是编造的入口。
2. 摘要还原是否正确（OpenAlex 存的是倒排索引，还原错了会得到一堆乱词）。

用法：
    python tools/smoke_websearch.py
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"E:\AI\thesis-suite")

QUERIES = ["rural teacher burnout organizational support", "乡村教师 职业倦怠 组织支持"]


def main() -> int:
    from suite import websearch

    print("=" * 74)
    print("互联网文献检索层验证")
    print("=" * 74)
    print(f"允许的主机白名单：{len(websearch.ALLOWED_HOSTS)} 个")
    print(f"礼貌池邮箱：{websearch.DEFAULT_MAILTO or '（未设置，可能更易被限流）'}")

    # ---- 1. 白名单护栏：伪造地址必须被拒 ----
    print("\n--- 安全护栏：非白名单地址必须被拒绝 ---")
    import os

    os.environ["SUITE_CROSSREF_BASE"] = "https://evil.example.com"
    try:
        websearch.Crossref()
        print("  [FAIL] 非白名单地址竟然被接受了")
        os.environ.pop("SUITE_CROSSREF_BASE")
        return 1
    except ValueError as exc:
        print(f"  [OK]   已拒绝：{exc}")
    finally:
        os.environ.pop("SUITE_CROSSREF_BASE", None)

    # ---- 2. 逐源检索 ----
    failures: list[str] = []
    for cls in websearch.SOURCES:
        print(f"\n--- {cls.name} ---")
        try:
            adapter = cls()
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] 构造失败：{exc}")
            failures.append(cls.name)
            continue

        started = time.time()
        try:
            records = adapter.search(QUERIES[0], limit=4)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] 检索失败：{type(exc).__name__}: {str(exc)[:200]}")
            failures.append(cls.name)
            continue

        took = time.time() - started
        usable = [r for r in records if r.usable]
        print(f"  返回 {len(records)} 条，可用（有题名+摘要）{len(usable)} 条，耗时 {took:.1f}s")
        for rec in records[:3]:
            flag = "可用" if rec.usable else "无摘要"
            print(f"    [{flag}] {rec.title[:56]}")
            print(f"           年={rec.year or '-'} DOI={rec.doi or '-'} 作者={(rec.authors or '-')[:36]}")
            if rec.abstract:
                print(f"           摘要 {len(rec.abstract)} 字：{rec.abstract[:90]}…")
        if not records:
            failures.append(cls.name)

    # ---- 3. 聚合入口 ----
    print("\n--- search_literature（聚合 + 去重 + 排序）---")
    started = time.time()
    found, status = websearch.search_literature(QUERIES, limit_per_source=3, max_total=10)
    print(f"合计 {len(found)} 条可用记录，耗时 {time.time() - started:.1f}s")
    for s in status:
        if s.get("ok"):
            print(f"  {s['source']:16} 查到 {s.get('found', 0):>2} 条，采用 {s.get('used', 0):>2} 条（{s.get('seconds', '?')}s）")
        else:
            print(f"  {s['source']:16} 失败：{str(s.get('error'))[:80]}")

    if found:
        print("\n  前 5 条（有 DOI 优先、其次按被引数）：")
        for rec in found[:5]:
            cites = f"被引 {rec.citations}" if rec.citations is not None else ""
            print(f"    · [{rec.source}] {rec.title[:52]}")
            print(f"      {rec.year or '?'} | {rec.venue[:26] or '-'} | {cites} | doi={rec.doi or '-'}")
            print(f"      生成的引用串：{rec.citation()[:110]}")
    else:
        print("  [FAIL] 一条可用记录都没拿到")

    # ---- 4. 摘要质量：确认不是倒排索引的乱码 ----
    print("\n--- 摘要质量抽查（防倒排索引还原错误）---")
    bad = 0
    for rec in found[:6]:
        words = rec.abstract.split()
        # 正常英文摘要有常见虚词；如果全是短词碎片，说明还原错了
        looks_ok = len(rec.abstract) > 100 and any(
            w.lower().strip(".,;:") in {"the", "of", "and", "in", "to", "for", "with", "a", "is", "we", "this"}
            for w in words[:40]
        )
        mark = "OK  " if looks_ok else "可疑"
        if not looks_ok:
            bad += 1
        print(f"  [{mark}] {rec.abstract[:100]}…")
    if bad:
        print(f"  [FAIL] {bad} 条摘要看起来不正常")
        failures.append("摘要质量")

    print("\n" + "=" * 74)
    if failures:
        print(f"有问题的源：{failures}")
        print("注意：某个源失败可能只是限流或网络，不一定代表代码有问题。")
        return 1
    print(f"全部通过：{len(found)} 条可用记录，四源均可用，摘要还原正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
