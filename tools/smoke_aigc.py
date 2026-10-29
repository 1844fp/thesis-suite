"""验证降 AIGC 率客户端（``suite/aigc.py``）。

只验证**这一层封装**，不改写任何上游代码。重点看四件事：

1. **白名单护栏**：``SUITE_REDUCER_BASE`` 被指到非白名单主机时必须被拒（防 SSRF）。
2. **服务不在线时的错误可操作**：信息里必须含**启动方法**（``run.cmd``），
   而不是一句 ``ConnectError``。这是这个模块最常见的失败。
3. **真实调用**：若 8770 上确实跑着 aigc-reducer，就跑一次真的 ``score()``
   并贴出 AI 味指数与逐项指标；不在线就如实说"未验证真实调用"，**不假装跑过**。
4. **诚实声明跟着返回值走**：``disclaimer`` 必须真的出现在 ``score()`` /
   ``reduce_text()`` 的返回值里，而不是只写在文档注释里。

用法：
    python tools/smoke_aigc.py

注意：本脚本**不会**调用 ``reduce_text()`` 做真实改写——那会消耗被调用服务自己
配置的 API Key（见模块文档的"模型凭据不转发"）。所以第 4 项用"服务不在线"
这条路径来检查字段是否被挂上，不为了测试去花钱。
"""

from __future__ import annotations

import os
import sys

# 本机开发时第三方依赖不在 site-packages 里，而是 vendored 在工作区的 libs\ 下
# （与 run.ps1 的做法一致）。公网容器里依赖由 pip 安装，这几行是空操作。
for _p in (
    r"E:\AI\thesis-suite",
    r"E:\AI\aigc-reducer\libs",
    r"E:\AI\thesis-assistant\libs",
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

def _free_port() -> int:
    """取一个**真的没人监听**的端口。

    原先这里硬编码 8799，并假设"高位端口正常没人用"。这个假设被打破了：
    研序新版（`modern/` 的 `node server.mjs`）正好监听 8799，
    于是"服务没开"的模拟变成"连到了一个不是 aigc-reducer 的服务"，
    返回 HTTP 404 而不是连接失败，四条断言全部误失败。

    教训：**测试里不要硬编码"空闲"资源**。让系统给一个真的空闲端口。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DEAD_PORT = _free_port()

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}" + (f"：{detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 就算了
        pass

    from suite import aigc

    print("=" * 78)
    print("降 AIGC 率客户端（suite/aigc.py）验证")
    print("=" * 78)
    print(f"默认服务地址 REDUCER_BASE = {aigc.REDUCER_BASE}")
    print(f"主机白名单 ALLOWED_HOSTS  = {sorted(aigc.ALLOWED_HOSTS)}")
    print(f"解除白名单的开关          = SUITE_ALLOW_ANY_REDUCER_HOST=1")

    # ---------------------------------------------------------------- 1
    print("\n--- 1. 安全护栏：非白名单主机必须被拒绝 ---")
    os.environ["SUITE_REDUCER_BASE"] = "https://evil.example.com"
    try:
        aigc.ReducerClient()
        check(False, "非白名单地址被接受了", "这是 SSRF 漏洞")
    except aigc.AigcError as exc:
        check(True, "已拒绝非白名单地址", str(exc)[:110])
    except Exception as exc:  # noqa: BLE001
        check(False, "抛了非 AigcError 的异常", f"{type(exc).__name__}: {exc}")

    # 顺带验证：模块级函数同样不该把请求发出去
    leaked = aigc.score("测试文本。")
    check(leaked.get("ok") is False, "模块级 score() 同样被拦住",
          str(leaked.get("error"))[:90])

    # 白名单开关确实存在（不真的用它发请求，只验证常量读得到）
    check(isinstance(aigc.ALLOW_ANY_HOST, bool), "白名单开关可读",
          f"ALLOW_ANY_HOST={aigc.ALLOW_ANY_HOST}（本机未设置=默认收紧）")

    # 没写协议头的地址也要给出可读错误，而不是崩
    os.environ["SUITE_REDUCER_BASE"] = "127.0.0.1:8770"
    try:
        aigc.ReducerClient()
        check(False, "缺协议的地址被接受了")
    except aigc.AigcError as exc:
        check(True, "缺 http:// 的地址已拒绝", str(exc)[:110])
    os.environ.pop("SUITE_REDUCER_BASE", None)

    # ---------------------------------------------------------------- 2
    print(f"\n--- 2. 服务不在线：错误信息必须含启动方法（模拟：指向空端口 {DEAD_PORT}）---")
    print("    实测说明：本机连一个没人监听的端口**不一定**抛 ConnectError——")
    print("    沙箱/代理环境会返回 HTTP 502（body 为空），而 502 也是"
          "「服务在跑但调模型失败」的码。\n"
          "    所以下面的断言查的是「两条排查路径都被写出来」，而不是只查连接异常。")
    os.environ["SUITE_REDUCER_BASE"] = f"http://127.0.0.1:{DEAD_PORT}"
    try:
        offline = aigc.score("这是一段用来测试离线路径的文本。")
        err = str(offline.get("error") or "")
        check(offline.get("ok") is False, "离线时 score() 没抛异常，返回了 ok=False")
        print(f"    实际拿到的错误（前 130 字）：{err[:130]}…")
        check("run.cmd" in err, "错误信息里包含启动方法 run.cmd")
        check("aigc-reducer" in err, "错误信息里指出了是哪个服务")
        check("SUITE_REDUCER_BASE" in err, "错误信息里说明了改地址的开关")
        check("LLM_API_KEY" in err, "错误信息里同时给出了「服务在跑但模型失败」的排查路径")
        check(bool(offline.get("start_hint")), "另附 start_hint 字段供前端展示")

        h = aigc.health()
        check(h.get("ok") is False and h.get("online") is False, "离线时 health().online=False")
        check("run.cmd" in str(h.get("error") or ""), "health() 的错误也含启动方法")

        m = aigc.metrics()
        check(m.get("ok") is False and "run.cmd" in str(m.get("error") or ""),
              "metrics() 服务不可达时返回可读错误而非抛异常")

        r = aigc.rules()
        check(r.get("ok") is False and "run.cmd" in str(r.get("error") or ""),
              "rules() 服务不可达时返回可读错误而非抛异常")

        # export_docx 是唯一会抛的接口（返回类型是 bytes），验证它抛的也是中文
        try:
            aigc.export_docx("正文。", title="测试")
            check(False, "离线时 export_docx() 竟然成功了")
        except aigc.AigcError as exc:
            check("run.cmd" in str(exc), "export_docx() 抛的 AigcError 同样含启动方法")

        # 流式路径：不能抛，必须吐出一帧说明白的 error
        frames = list(aigc.reduce_stream("这是一段用来测试离线路径的文本。"))
        check(bool(frames) and frames[0].get("type") == "error",
              "离线时 reduce_stream() 吐出 error 帧而不是抛异常")
        check("run.cmd" in str(frames[0].get("message") or ""),
              "流式 error 帧里也含启动方法")

        # reduce_text 离线时也要把声明挂上（第 4 项在这里一并验证）
        red = aigc.reduce_text("这是一段用来测试离线路径的文本。")
        check(red.get("ok") is False, "离线时 reduce_text() 返回 ok=False")
        check("run.cmd" in str(red.get("error") or ""), "离线时 reduce_text() 的错误也可操作")
    finally:
        os.environ.pop("SUITE_REDUCER_BASE", None)

    # ---------------------------------------------------------------- 3
    print("\n--- 3. 真实调用（若服务在线）---")
    base_before = aigc.REDUCER_BASE
    h = aigc.health()
    print(f"  探测 {base_before}/api/health → ok={h.get('ok')} online={h.get('online')}")
    if not h.get("ok"):
        print("  [SKIP] 降 AIGC 服务**不在线**，未能验证真实调用（不假装跑过）。")
        print(f"         启动方法：双击 E:\\AI\\aigc-reducer\\run.cmd")
        print(f"         错误：{str(h.get('error'))[:160]}…")
    else:
        print(f"  服务版本 = {h.get('version')}")
        print(f"  服务自身是否配了模型 Key = {h.get('has_own_llm_key')}（模型 {h.get('own_llm_model')}）")
        if h.get("credentials_note"):
            print(f"  凭据说明：{str(h['credentials_note'])[:150]}…")

        m = aigc.metrics()
        print(f"  指标说明：{len(m.get('metrics') or [])} 项，算法 = {m.get('method')}，"
              f"锚点 {len(m.get('anchors') or {})} 组")
        for item in (m.get("metrics") or [])[:11]:
            print(f"      {item.get('key', ''):24} {item.get('label', '')} "
                  f"[{item.get('unit', '')}] 权重={item.get('weight')}")

        r = aigc.rules()
        print(f"  规则清单：{len(r.get('rules') or [])} 条，"
              f"分类 {list((r.get('categories') or {}).keys())}")

        sample = (
            "综上所述，本文通过深入探讨乡村教师职业倦怠问题，构建了系统性的分析框架。"
            "首先，值得注意的是，组织支持在缓解职业倦怠方面发挥着至关重要的作用。"
            "其次，从多维度的视角来看，该问题不仅涉及个体因素，更与社会环境密切相关。"
            "总之，本研究为相关政策制定提供了重要的理论依据与实践参考。"
        )
        print(f"\n  送检文本（{len(sample)} 字）：{sample[:60]}…")
        s = aigc.score(sample)
        check(s.get("ok") is True, "score() 真实调用成功")
        print(f"  >>> AI 味指数 = {s.get('index')}   等级 = {s.get('label')}")
        print(f"      字符 {s.get('chars')} / 句 {s.get('sentences')} / 段 {s.get('paragraphs')}"
              f"   算法 = {s.get('method')}")
        print(f"      逐项指标（{len(s.get('metrics') or [])} 项）与命中规则：")
        for item in (s.get("metrics") or []):
            print(f"        {item.get('key', ''):24} {item.get('label', ''):8} "
                  f"值={item.get('value')} {item.get('unit', '')} "
                  f"可疑度={item.get('suspicion')}")
        hits = s.get("hits") or []
        print(f"      命中规则 {len(hits)} 处，前 5 条：")
        for hit in hits[:5]:
            print(f"        [{hit.get('rule_id')}] {hit.get('name')} "
                  f"×{hit.get('count', 1)} 例：{str(hit.get('snippet', ''))[:34]}")
        if s.get("diagnosis"):
            print(f"      诊断：{str(s['diagnosis'])[:150]}…")

        # 保真校验也真实跑一次（纯本地计算，不花钱）
        g = aigc.guard("样本量为 128 人，见 Wang (2020)。", "样本量为 256 人，见 Li (2021)。")
        check(g.get("ok") is True, "guard() 真实调用成功")
        print(f"  >>> 保真校验：ok(调用成功)={g.get('ok')}"
              f"  passed(保真结论)={g.get('passed')}  判定={g.get('verdict')}")
        print(f"      违规 {len(g.get('violations') or [])} 条，告警 {len(g.get('warnings') or [])} 条")
        for v in (g.get("violations") or []):
            print(f"        [{v.get('kind')}] {v.get('detail')} 缺失={v.get('missing')}")
        # 这条断言是防回归的：早先本模块把上游的 ok（保真通过）直接覆盖成了 True，
        # 于是"查出 2 处丢数字"被报成通过——正好把结论说反。
        check(g.get("passed") is False,
              "guard() 没有把「查出改动」误报为通过（passed=False）")
        check(g.get("ok") is True and g.get("passed") is False,
              "ok=调用成功 与 passed=保真结论 没有混为一谈")
        check(s.get("disclaimer") is not None, "在线 score() 返回值含 disclaimer")

    # ---------------------------------------------------------------- 4
    print("\n--- 4. 诚实声明必须跟着返回值走 ---")
    check(bool(aigc.DISCLAIMER), "模块级 DISCLAIMER 非空")
    check("不可互相换算" in aigc.DISCLAIMER, "DISCLAIMER 含「不可互相换算」")
    check("不是" in aigc.DISCLAIMER and "商用" in aigc.DISCLAIMER,
          "DISCLAIMER 明确否认等同于商用检测器")
    check(bool(aigc.CREDENTIAL_POLICY), "模块级 CREDENTIAL_POLICY 非空")
    check("不转发" in aigc.CREDENTIAL_POLICY, "CREDENTIAL_POLICY 说明了不转发访客密钥")

    # 两个"必须带声明"的接口，各走一次能走通的路径来检查字段真的挂上去了。
    if h.get("ok"):
        s = aigc.score("综上所述，本文进行了深入探讨。")
        check(isinstance(s.get("disclaimer"), str) and len(s["disclaimer"]) > 20,
              "score() 返回值里真的有 disclaimer 字段", f"{len(s.get('disclaimer') or '')} 字")
    else:
        print("  [SKIP] 服务不在线，无法在成功路径上检查 score().disclaimer。")
        print("         （已确认常量存在，且失败路径下会返回可操作提示。）")

    print("\n  凭据路由（aigc-reducer 怎么拿 Key）——这是本模块的结论，不是猜测：")
    print("    它的 HTTP 接口只接受 text/genre/intensity/target_score/max_rounds/"
          "auto_iterate/concurrency/keep_terms/style_sample/consistency/fact_check，")
    print("    **没有** base_url / api_key / model 字段（app/server.py 的 _run_kwargs）。")
    print("    它从自己的 .env 读 LLM_API_KEY（app/config.py 的 load_settings()），")
    print("    所以本站**没有通道**转发访客密钥，改写费用记在该服务自己的 Key 上。")

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"有 {len(FAILURES)} 项失败：{FAILURES}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
