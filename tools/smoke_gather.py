"""资料获取助手的端到端冒烟测试（不启动服务器，直接用 TestClient）。

为什么要有 4 号「假模型」用例
----------------------------
真实的 ``/extract`` 需要一个能用的 API Key，而这台机器上没有。于是本脚本把
``Chat`` 换成返回**预置 JSON** 的假客户端，把抽取路径里的关键不变量钉死：

* ``passages`` 里那句原文里根本没有的话，必须被丢掉并计入 ``dropped_passages``
  （这是本助手的核心价值，绝不能只靠提示词约束模型）；
* 长文按切片多次调用，第二片抽到的片段不能被第一片的丢弃逻辑连带杀掉；
* 超过 20000 字的正文被截断而不是被拒绝，并且如实标注 ``truncated``。

真实网络只用在 6 号 ``/verify``：DOI 核验走 Crossref，不需要 Key。**这条会真的联网**，
跑不通时脚本会如实说"未通过"，不会假装成功。

用法::

    $env:PYTHONPATH = "E:\\AI\\thesis-suite;E:\\AI\\aigc-reducer\\libs;E:\\AI\\thesis-assistant\\libs"
    $env:PYTHONUTF8 = "1"
    python tools\\smoke_gather.py
"""

from __future__ import annotations

import io
import sys
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, r"E:\AI\thesis-suite")

from fastapi.testclient import TestClient  # noqa: E402

from suite.api import gather as gather_mod  # noqa: E402

#: 一条真实存在、结构规范的英文文献（LeCun/Bengio/Hinton, Nature 2015）。
REAL_REF = "[1] LE CUN Y, BENGIO Y, HINTON G. Deep learning[J]. Nature, 2015, 521(7553): 436-444."

#: 一条真实存在、带 DOI 的会议论文。
REAL_DOI = "10.1145/3292500.3330701"

#: 一条中文文献：Crossref 基本不收录，用来确认"查不到"不会崩、也不会被说成造假。
CN_REF = "[2] 张三, 李四. 深度学习在文本分类中的应用研究[J]. 计算机应用研究, 2021, 38(4): 1024-1029."

#: 一条**确实不存在**的文献：DOI 是编的，题名也是刻意编造的生造词串。
#:
#: 为什么题名要生造到这种程度：核验器在 DOI 查不到时会**退回按标题检索**，
#: 而标题相似度是词级序列比。像 "A totally fabricated survey of nothing at all"
#: 这种"看着编造、用词却常见"的标题，会以 0.93 的相似度命中一篇真实的
#: "Nothing At All"（实测），于是被记成 ``confirmed``。生造词串才能稳定落进
#: ``not_found``——顺带说明这个助手真正能保证的是"标题/DOI 与真实记录对得上"，
#: 而不是"这篇文献的结论是真的"。
FAKE_REF = (
    "[3] ZZYZX Q. Quixotic wampeter frobnication of purple unicorns[J]. "
    "Journal of Imaginary Studies, 2019, 12(3): 45-58. DOI: 10.99999/jis.2019.00001."
)

SAMPLE_TEXT = (
    "深度学习通过多层表示学习从数据中自动提取特征。本文提出一种新的文本分类方法，"
    "在三个公开数据集上进行了实验。实验结果表明，该方法的准确率达到 95.3%，"
    "相比基线模型提升了 2.1 个百分点。我们认为注意力机制是性能提升的主要来源。"
)

FAKE_CHAT_JSON = {
    "title": "深度学习在文本分类中的应用研究",
    "authors": "张三、李四",
    "year": "2021",
    "doi": "10.1234/fake.2021.001",
    "source": "计算机应用研究",
    "type": "期刊论文",
    "passages": [
        "深度学习通过多层表示学习从数据中自动提取特征",
        # 下面这句原文里**没有**，必须被校验拦下并计入 dropped_passages。
        "本文的方法在 ImageNet 上取得了 99.9% 的准确率",
    ],
    "claims": ["注意力机制是性能提升的主要来源"],
}

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def show(label: str, value: object, limit: int = 400) -> None:
    text = str(value)
    print(f"  {label}: {text[:limit]}{'…' if len(text) > limit else ''}")


class FakeChat:
    """假模型客户端：按切片顺序轮流吐预置 JSON，并记录每次收到的 user 消息。

    ``tag`` 用来给不同来源的抽取结果造出不同题名/DOI——证据库按
    ``doi/url/title`` 去重（见 ``sessions.add_evidence``），三条资料若给出同样的
    字段会被合并成一条，那是**正确的**行为，但会让本用例的断言失去意义。
    """

    def __init__(self, *args, tag: str = "", **kwargs) -> None:
        self.calls: list[str] = []
        self.tag = tag

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ask_json(self, messages, **kwargs):
        user = ""
        for message in messages:
            if message.get("role") == "user":
                user = str(message.get("content") or "")
        self.calls.append(user)
        index = len(self.calls) - 1
        # 从提示词里把访客给的题名/DOI/正文再读出来，让不同来源产出**不同**字段。
        # 证据库按 doi/url/title 去重（见 sessions.add_evidence）：假模型若对每条
        # 来源都返回同一套字段，三条会被正确地合并成一条，本用例就白测了。
        title = _prompt_field(user, "用户给出的题名（可能为空）：")
        doi = _prompt_field(user, "用户给出的 DOI（可能为空）：")
        payload = dict(FAKE_CHAT_JSON)
        if title:
            # 访客给了题名/DOI 时，假模型就照抄它们（真实实现也是"访客优先"），
            # 这样同一批请求里每条来源的字段天然互不相同。
            payload["title"] = title
            payload["doi"] = doi
        elif self.tag:
            payload["title"] = f"{FAKE_CHAT_JSON['title']}-{self.tag}"
            payload["doi"] = f"10.1234/fake.{self.tag}"
        if index == 0:
            # 第一片：只给一个真实片段 + 一个编造片段，考验逐字校验。
            payload["passages"] = [FAKE_CHAT_JSON["passages"][0], FAKE_CHAT_JSON["passages"][1]]
        else:
            # 第二片起：给后文里逐字存在的片段，确认切片循环不会把后段内容丢掉。
            payload["passages"] = ["在该数据集上准确率达到 95.3%"]
        return payload

    def ask(self, messages, **kwargs):  # pragma: no cover - 抽取路径不用 ask
        return ""


def _prompt_field(prompt: str, label: str) -> str:
    """从假模型收到的提示词里取出某个字段的值（只用于测试替身）。"""
    for line in prompt.splitlines():
        if line.startswith(label):
            value = line[len(label) :].strip()
            return "" if value == "（未提供）" else value
    return ""


def main() -> int:
    from suite.api.app import create_app

    client = TestClient(create_app())
    headers = {"X-LLM-Key": "sk-test", "X-LLM-Base": "https://api.deepseek.com/v1"}

    # ---- 1. status（无 sid）------------------------------------------------
    print("\n=== 1. GET /api/gather/status（无 sid）===")
    resp = client.get("/api/gather/status")
    show("HTTP", resp.status_code)
    show("body", resp.text, 300)
    data = resp.json() if resp.status_code == 200 else {}
    check("无 sid 也返回 200（不报 404）", resp.status_code == 200)
    check("evidence_count 为 0", data.get("evidence_count") == 0)
    check("items 是列表", isinstance(data.get("items"), list))

    # ---- 2. extract 不带任何 LLM 头 ---------------------------------------
    print("\n=== 2. POST /api/gather/extract（不带 LLM 头）===")
    resp = client.post("/api/gather/extract", json={"sources": [{"text": SAMPLE_TEXT}]})
    show("HTTP", resp.status_code)
    show("body", resp.text, 300)
    check("返回 400", resp.status_code == 400)
    check("消息可读（提到 API Key）", "API Key" in resp.text)

    # ---- 3. extract 带假 Key（必须失败但不炸栈）---------------------------
    print("\n=== 3. POST /api/gather/extract（假 Key，真实调模型）===")
    started = time.time()
    resp = client.post(
        "/api/gather/extract", json={"sources": [{"text": SAMPLE_TEXT}]}, headers=headers
    )
    show("HTTP", resp.status_code)
    show("body", resp.text, 400)
    show("耗时(s)", round(time.time() - started, 1))
    check("不是 500（没有 traceback）", resp.status_code != 500)
    check("是可读的 4xx/5xx", 400 <= resp.status_code < 600)
    check("错误信息非空且不含 traceback 字样", "Traceback" not in resp.text and len(resp.text) > 20)

    # ---- 4. extract 用假模型（可离线、可断言）-----------------------------
    print("\n=== 4. POST /api/gather/extract（假 Chat，验证逐字校验/截断/切片）===")
    real_chat = gather_mod.llm_mod.Chat
    gather_mod.llm_mod.Chat = FakeChat
    try:
        short_text = SAMPLE_TEXT
        resp = client.post(
            "/api/gather/extract",
            json={"sources": [{"title": "用户给的题名", "text": short_text, "doi": ""}]},
            headers=headers,
        )
        show("HTTP（短文）", resp.status_code)
        show("body（短文）", resp.text, 900)
        data = resp.json() if resp.status_code == 200 else {}
        check("短文抽取 200", resp.status_code == 200)
        check("added = 1", data.get("added") == 1)
        check(
            "编造片段被丢弃，dropped_passages = 1",
            data.get("dropped_passages") == 1,
            f"实际 {data.get('dropped_passages')}",
        )
        sid = data.get("sid") or ""
        items = data.get("items") or []
        check("返回了 sid", bool(sid))
        if items:
            item = items[0]
            check("只保留通过校验的片段", len(item["passages"]) == 1, str(item["passages"]))
            check("passages 都在原文里", all(p in short_text for p in item["passages"]))
            check("题名用访客给的（优先于模型）", item["title"] == "用户给的题名")
            check("verified 初始为空", item["verified"] == "")
            check("origin = user", item["origin"] == "user")
            check("citation 非空", bool(item["citation"]), item["citation"])

        # 长文：>20000 字 → 截断 + 切片多次调用。
        # 关键片段刻意**放在第 20000 字附近**：既落在第一片之外（证明切片循环没丢后段），
        # 又在截断线之内（证明我们截的是尾巴而不是把有用的中段扔掉）。
        target = "在该数据集上准确率达到 95.3%"
        filler = "补充说明内容。"
        head = short_text + filler * 500          # 约 5000 字
        pad = filler * ((19_000 - len(head)) // len(filler))
        long_text = head + pad + target + filler * 200
        check(
            "长文用例构造正确（目标片段在截断线内、第一片之外）",
            len(long_text) > gather_mod.MAX_TEXT
            and 8_000 < long_text.index(target) < gather_mod.MAX_TEXT,
            f"正文 {len(long_text)} 字，片段在第 {long_text.index(target)} 字",
        )
        spy = FakeChat()
        gather_mod.llm_mod.Chat = lambda *a, **k: spy
        resp = client.post(
            "/api/gather/extract", json={"sources": [{"text": long_text}]}, headers=headers
        )
        show("HTTP（长文）", resp.status_code)
        check("长文抽取 200", resp.status_code == 200)
        check("长文被多次切片调用", len(spy.calls) > 1, f"调用 {len(spy.calls)} 次")
        if resp.status_code == 200:
            data = resp.json()
            check("长文标注 truncated", bool(data["items"][0]["truncated"]))
            check(
                "后段片段也被抽到（切片没丢内容）",
                target in data["items"][0]["passages"],
                str(data["items"][0]["passages"]),
            )
            check(
                "正文被截断到 20000 字以内",
                sum(len(p) for p in data["items"][0]["passages"]) <= gather_mod.MAX_TEXT,
            )
        gather_mod.llm_mod.Chat = FakeChat

        # 单条失败不拖垮整批：让第一片调用抛 LLMError。
        class FlakyChat(FakeChat):
            def ask_json(self, messages, **kwargs):
                raise gather_mod.llm_mod.LLMError("模拟的模型故障")

        gather_mod.llm_mod.Chat = FlakyChat
        resp = client.post(
            "/api/gather/extract", json={"sources": [{"text": SAMPLE_TEXT}]}, headers=headers
        )
        show("HTTP（全部失败）", resp.status_code)
        show("body（全部失败）", resp.text, 300)
        check("全失败时不以 200 结束", resp.status_code >= 400)
        check("全失败时错误可读", "模型故障" in resp.text)
    finally:
        gather_mod.llm_mod.Chat = real_chat

    # ---- 5. 参数校验：sources 超限 / 空 text ------------------------------
    print("\n=== 5. 参数校验 ===")
    many = [{"text": "x" * 10} for _ in range(gather_mod.MAX_SOURCES + 1)]
    resp = client.post("/api/gather/extract", json={"sources": many}, headers=headers)
    show("HTTP（21 条）", resp.status_code)
    check("sources 超过上限 → 413", resp.status_code == 413)
    resp = client.post("/api/gather/extract", json={"sources": [{"text": "   "}]}, headers=headers)
    show("HTTP（空 text）", resp.status_code)
    check("空 text → 400", resp.status_code == 400)

    # ---- 6. verify（真实联网 Crossref，不需要 Key）------------------------
    print("\n=== 6. POST /api/gather/verify（真实联网 Crossref）===")
    # 先造三条证据。抽取这一步走假模型（本机没有可用的真 Key），
    # 随后把 DOI/标题**直接写进证据库**，好让核验对象完全可控。
    # 每条来源都带一个不同的占位 DOI：证据库按 doi/url/title 去重
    # （见 sessions.add_evidence），字段相同的三条会被**正确地**合并成一条。
    gather_mod.llm_mod.Chat = FakeChat
    try:
        resp = client.post(
            "/api/gather/extract",
            json={
                "sources": [
                    {"title": f"待核验条目 {i + 1}", "doi": f"10.0000/seed.{i + 1}", "text": ref}
                    for i, ref in enumerate([REAL_REF, CN_REF, FAKE_REF])
                ]
            },
            headers=headers,
        )
    except Exception as exc:  # noqa: BLE001
        print("  准备证据失败：", exc)
        resp = None
    finally:
        gather_mod.llm_mod.Chat = real_chat

    if resp is None or resp.status_code != 200:
        check("准备核验用证据", False, f"HTTP {getattr(resp, 'status_code', '?')}")
    else:
        vsid = resp.json()["sid"]
        session = gather_mod.STORE.get(vsid)
        check("证据库里有 3 条互不相同的证据", len(session.evidence) == 3, f"{len(session.evidence)} 条")
        seeds = [
            # (题名, 作者, 年份, 来源, 送去核验的 DOI)
            ("Deep learning", "LE CUN Y, BENGIO Y, HINTON G", "2015", "Nature", REAL_DOI),
            ("深度学习在文本分类中的应用研究", "张三, 李四", "2021", "计算机应用研究", ""),
            (
                "Zzyzx quixotic wampeter frobnication of purple unicorns",
                "ZZYZX Q",
                "2019",
                "Journal of Imaginary Studies",
                "10.99999/jis.2019.00001",
            ),
        ]
        for item, (title, authors, year, source, doi) in zip(session.evidence, seeds):
            item["title"] = title
            item["authors"] = authors
            item["year"] = year
            item["source"] = source
            item["doi"] = doi
            item["citation"] = f"{authors}. {title}[J]. {source}, {year}."

        started = time.time()
        resp = client.post("/api/gather/verify", json={"sid": vsid, "limit": 10})
        show("HTTP", resp.status_code)
        show("耗时(s)", round(time.time() - started, 1))
        show("body", resp.text, 1400)
        check("verify 不需要 Key 也能跑（200）", resp.status_code == 200)
        if resp.status_code == 200:
            data = resp.json()
            tally = data.get("tally") or {}
            check("tally 里有 confirmed", "confirmed" in tally, str(tally))
            check("tally 里有 not_found", "not_found" in tally, str(tally))
            check(
                "编造的 DOI 没有被说成 confirmed",
                next(
                    (r["verdict"] for r in data["items"] if r["doi"] == "10.99999/jis.2019.00001"),
                    "?",
                )
                == "not_found",
                str(tally),
            )
            check("checked = 3", data.get("checked") == 3, str(data.get("checked")))
            check(
                "verified 字段写回了证据库",
                all(gather_mod._is_verified(i) for i in session.evidence),
            )
            for row in data.get("items") or []:
                print(f"    · {row['verdict']:<13} {row['title'][:40]:<42} {row['detail'][:60]}")

            # 再跑一次：已核验的条目不该被重复查询。
            resp2 = client.post("/api/gather/verify", json={"sid": vsid, "limit": 10})
            show("HTTP（二次）", resp2.status_code)
            show("body（二次）", resp2.text, 300)
            check("已核验条目不重复查", resp2.status_code == 200 and resp2.json()["checked"] == 0)

        # ---- 7. export 三种格式 ------------------------------------------
        print("\n=== 7. POST /api/gather/export（三种格式）===")
        for fmt in ("gbt", "bibtex", "markdown"):
            resp = client.post("/api/gather/export", json={"sid": vsid, "format": fmt})
            headers_out = dict(resp.headers)
            body = resp.text
            show(f"HTTP({fmt})", resp.status_code)
            show(f"Content-Disposition({fmt})", headers_out.get("content-disposition", ""))
            check(f"{fmt} 导出 200", resp.status_code == 200)
            check(f"{fmt} 内容非空", len(body.strip()) > 20, f"{len(body)} 字符")
            check(
                f"{fmt} 带 RFC5987 附件文件名",
                "filename*=UTF-8''" in headers_out.get("content-disposition", ""),
            )
            print(f"    {fmt} 前 260 字：")
            print("      " + body[:260].replace("\n", "\n      "))
        resp = client.post("/api/gather/export", json={"sid": vsid, "format": "docx"})
        show("HTTP（非法格式）", resp.status_code)
        check("非法格式 → 400", resp.status_code == 400)

        # ---- 8. status 有 sid / DELETE -----------------------------------
        print("\n=== 8. status(有 sid) 与 DELETE /evidence ===")
        resp = client.get("/api/gather/status", params={"sid": vsid})
        show("HTTP", resp.status_code)
        show("body", resp.text, 500)
        check("有 sid 时返回条数与视图", resp.status_code == 200 and resp.json()["evidence_count"] == 3)
        check("视图含 title/year/doi/verified", all(
            k in (resp.json()["items"][0] if resp.json()["items"] else {}) for k in ("title", "year", "doi", "verified")
        ))
        resp = client.delete("/api/gather/evidence", params={"sid": vsid})
        show("HTTP（DELETE）", resp.status_code)
        show("body（DELETE）", resp.text, 200)
        check("DELETE 返回 removed=3", resp.status_code == 200 and resp.json().get("removed") == 3)
        resp = client.get("/api/gather/status", params={"sid": vsid})
        check("清空后 evidence_count=0", resp.json().get("evidence_count") == 0)

    # ---- 汇总 -------------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name in FAILED:
        print("  FAIL:", name)
    if not FAILED:
        print("全部通过。")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
