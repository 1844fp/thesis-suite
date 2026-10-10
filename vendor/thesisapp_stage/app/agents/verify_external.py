"""能力⑥「资料和数据验证」——外部核验智能体 + 静态数据一致性检查。

# 这个智能体为什么存在

**AI 编造参考文献是硕士论文最致命的错误之一**，而且极难自查：条目格式看起来
完全正常，作者、年份、卷期页一应俱全。作者本人也无从判断——他可能只是看到
一个"像真的"条目就用上了。唯一能识破它的办法是**拿去真实数据库核对**。

因此这个智能体的使命只有一个：把稿件里每一条参考文献送到 Crossref / OpenAlex
核实，并把结论分成**四类**，而不是简单的"有/没有"：

==================  ==========================================================
``confirmed``       真实存在且匹配
``weak_match``      找到了相似文献但匹配度不够高，**需要作者人工确认**
``not_found``       所有源都查不到。严重度按语种分档（见下）
``inconclusive``    查询本身失败（断网 / 限流），**绝不可当成"文献不存在"**
==================  ==========================================================

第三类与第四类的区分是这套设计里最重要的一件事。把"断网"报成"你的文献是假的"，
比不报还糟糕——作者会开始怀疑一个本来正确的工具，然后关掉它。

# 为什么 ``uses_model = False``

核验结论**必须来自真实数据库，不能来自模型判断**。让模型"看一眼这条文献是否
真实存在"正好落在它最不可靠的地方：它会顺着条目的格式流畅地编出一个答案。
这个智能体只做查询和翻译，不做判断。

# 能跑一半断掉

60 条文献会发出上百次 HTTP 请求，中途断网、被限流是常态。所以每个条目**查完
立刻把结论写回项目 meta**，重跑时跳过已核验的条目。断点续验的键是条目原文的
sha1 指纹，不是序号——作者删掉前面一条时序号会整体平移，用序号做键会把 A 文献
的结论套到 B 文献头上。

# 保守性的来源

判定阈值本身不在这里，而在 ``app.adapters.research``（``MIN_RELEVANCE=0.55``
是实测标定出来的：编造标题的最佳匹配落在 0.27~0.44，真实中文标题 Crossref
0.65 / OpenAlex 0.10）。**这里不重写判定逻辑**，只负责"把结论翻译成框架的
Issue"。两处实现同一套判定迟早会分歧，而分歧的后果是把真实文献误报成编造。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Callable

from ..adapters.research import (
    CrossrefAdapter,
    ExternalVerifier,
    OpenAlexAdapter,
    PeerHTTP,
    WorkRecord,
)
from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage
from ..framework.registry import verifier
from .references_extract import LOOKUP_MIN_TITLE, RefEntry, parse_entries, split_reference_section

__all__ = [
    "CHECK_NAME",
    "ExternalVerifyAgent",
    "META_KEY",
    "VERDICT_CONFIRMED",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_NOT_FOUND",
    "VERDICT_WEAK",
    "check_citation_grounding",
    "citation_grounding_issues",
    "load_cached",
    "make_external_verifier",
    "verdict_to_issue",
]

#: 结论常量。用字符串而不是 Enum，因为要直接落进 project.json 的 meta。
VERDICT_CONFIRMED = "confirmed"
VERDICT_WEAK = "weak_match"
VERDICT_NOT_FOUND = "not_found"
VERDICT_INCONCLUSIVE = "inconclusive"

#: 项目 meta 里存放断点续验结果的键。
META_KEY = "external_verifications"


class _NoLookup:
    """占位"数据源"：条目既无 DOI 又无可检索标题时使用。

    存在的理由很实际：``ExternalVerifier.verify()`` 在**没有任何源**时会因为
    ``errors`` 为空而把结论定成 ``not_found``——也就是"文献不存在"。
    但缺标题**不等于**文献不存在，那只是我们抽不出可检索的字段。所以这里塞一个
    "永远无条件失败"的源，让 verify() 走 ``inconclusive`` 分支，把"查不了"和
    "查不到"分开。
    """

    label = "无可用检索字段"

    def by_doi(self, doi: str) -> WorkRecord:  # noqa: ARG002
        raise RuntimeError("条目缺少标题与 DOI，无法检索")

    def by_title(self, title: str) -> WorkRecord:  # noqa: ARG002
        raise RuntimeError("条目缺少标题与 DOI，无法检索")


def make_external_verifier(settings: Any) -> ExternalVerifier:
    """按配置构造真实的外部核验器。

    ``mailto`` 取自 ``settings.verify.contact_email``：Crossref 的礼貌池要求
    带上真实联系邮箱，否则高并发时会被限流——被限流的表现是 ``inconclusive``，
    也就是说**不填邮箱会平白多出一批"无法核验"**。
    """
    mailto = getattr(getattr(settings, "verify", None), "contact_email", "") or ""
    base = getattr(getattr(settings, "verify", None), "crossref_base", "") or "https://api.crossref.org"
    threshold = getattr(getattr(settings, "verify", None), "title_match_threshold", 0.82)
    return ExternalVerifier(
        crossref=CrossrefAdapter(base, mailto=mailto),
        openalex=OpenAlexAdapter(mailto=mailto),
        threshold=threshold,
    )


def load_cached(board: Any) -> dict[str, dict]:
    """读出项目 meta 里已核验的条目，返回 ``{指纹: 结果}``。

    兼容两种存储形态：list（新格式，保留查询顺序便于审计）与 dict（早期格式）。
    读到脏数据时返回空表而不是抛异常——一份写坏了的缓存不该让整个验证阶段崩掉。
    """
    raw = (getattr(board.project, "meta", None) or {}).get(META_KEY)
    out: dict[str, dict] = {}
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("fingerprint"):
                out[str(row["fingerprint"])] = row
    elif isinstance(raw, dict):
        for key, row in raw.items():
            if isinstance(row, dict):
                out[str(key)] = row
    return out


def _store_cache(board: Any, entries: dict[str, dict]) -> None:
    """把缓存写回 meta。按指纹排序，保证同样的输入产生同样的文件内容。"""
    board.project.meta[META_KEY] = [entries[k] for k in sorted(entries)]


def _skip_cached(cached: dict | None) -> bool:
    """已有结论时是否跳过这条。

    **只跳过"有定论"的条目**：``confirmed`` / ``weak_match`` / ``not_found``
    都是数据库给出的确定回答，重跑不会变，跳过能省下一半请求。

    ``inconclusive`` 必须重试——它代表"当时网络坏了"，不是关于这条文献的任何
    信息。把它缓存下来并跳过，会让一次偶发断网**永久**留在验证结果里。
    """
    if not cached:
        return False
    return cached.get("verdict") in (VERDICT_CONFIRMED, VERDICT_WEAK, VERDICT_NOT_FOUND)


def _snippet(text: str, limit: int = 80) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit] + "…"


def verdict_to_issue(entry: RefEntry, result: dict) -> Issue | None:
    """把一条核验结论翻译成框架的 Issue。``confirmed`` 返回 ``None``。

    严重度映射（``confirmed`` 不产生 Issue 是刻意的——正确情况不该刷屏，
    刷屏会让作者忽略真正的问题）：

    * ``not_found`` + 标题可信 → BLOCKER，疑似编造
    * ``not_found`` + 标题可疑 → MAJOR，**降级**，需要人工核对
    * ``weak_match``          → MAJOR，需要作者确认，并把**最相似文献的标题与相似度**
      写进 message。作者需要这个信息才能判断"是我记错了题目"还是"这条根本不存在"。
    * ``inconclusive``        → INFO，查询失败。**断网不是作者的错，不能报成 BLOCKER。**
    * 标题不可用 / 无 DOI     → INFO，说明无法核验的原因。

    # 为什么 ``not_found`` 要按标题可信度分档

    ``not_found`` 的语义是"两个数据库里都没有这个作品"。它成立的前提是**我们送去的
    标题确实等于作者的标题**。抽取只要有一点偏差（少一截、粘上刊名、把作者当题目），
    这个前提就不成立，硬报 BLOCKER 就变成"因为我的正则写错了，所以你的真实文献是
    编造的"。作者一旦被这样误导一次，就会永久关闭这个功能——那时候真正编造的文献
    也一起漏掉了。所以低可信标题上的 ``not_found`` 必须降级为"请人工核对"。
    """
    verdict = result.get("verdict", "")
    best = result.get("best") or {}
    where = f"第 {entry.index} 条" if entry.index else "未编号条目"
    label = _snippet(entry.title or entry.raw, 60)
    quality = entry.title_quality

    if verdict == VERDICT_CONFIRMED:
        return None

    # 标题根本不可检索：这是"我们查不了"，不是"文献不存在"。要先于 not_found 判断，
    # 否则一个抽取失败的条目会被当成编造。
    if quality == "unusable" and not entry.doi:
        return Issue(
            id="",
            kind="citation.unverified",
            severity=Severity.INFO,
            message=(
                f"参考文献 {where} 无法核验（条目缺少可检索的标题，也没有 DOI）：{_snippet(entry.raw, 100)}"
            ),
            location={"ref": entry.index},
            evidence=entry.raw,
            detected_by="external:precheck",
            fixable=False,
            fix_hint="补全题目与 DOI 后重跑外部核验；作者也可自行在知网/万方核对。",
        )

    if verdict == VERDICT_INCONCLUSIVE:
        note = best.get("note") or "; ".join(result.get("errors") or []) or "原因不明"
        return Issue(
            id="",
            kind="citation.unverified",
            severity=Severity.INFO,
            message=(
                f"参考文献 {where}「{label}」本次**未能核验**（查询失败：{_snippet(note, 120)}）。"
                "这不代表文献不存在，而是外部数据库这次没答上来；网络恢复后重跑即可。"
            ),
            location={"ref": entry.index},
            evidence=entry.raw,
            detected_by="external:crossref+openalex",
            fixable=False,
            fix_hint="检查网络或在设置里关闭外部核验；不要因为这条提示修改参考文献。",
        )

    if verdict == VERDICT_WEAK:
        score = best.get("score") or 0.0
        title = best.get("title") or "（无标题）"
        src = best.get("source") or "?"
        msg = (
            f"参考文献 {where}「{label}」只找到相似文献，匹配度不足，需要人工确认："
            f"最相似的是「{_snippet(title, 70)}」（{src}，相似度 {float(score):.2f}）。"
            "若这就是你要引用的文献，请据实更正条目信息。"
        )
        return Issue(
            id="",
            kind="citation.weak_match",
            severity=Severity.MAJOR,
            message=msg,
            location={"ref": entry.index},
            evidence=entry.raw,
            detected_by="external:crossref+openalex",
            fixable=False,
            fix_hint="核对题目、作者、年份是否与原文一致；不一致就改正，一致但数据库收录不全就忽略本条。",
        )

    if verdict == VERDICT_NOT_FOUND:
        note = best.get("note") or ""
        # 语种是比"标题可信度"更前置的一档判据。
        #
        # 实测标定（tools/demo.py + 直接查库）：三篇真实存在的中文期刊论文
        # （《会计研究》《管理世界》《中国工业经济》）在 Crossref 与 OpenAlex 的
        # 最佳匹配相似度只有 0.16~0.48——**不是抽取错误，是这两个库对中文期刊
        # 基本没有收录**。所以对中文标题，`not_found` 只能说明"这两个库没有"，
        # 不能说明"文献不存在"。把它报成 BLOCKER 等于一上来就冤枉每一条中文引用，
        # 用户会立刻不再相信这个功能——**误报的代价比漏报高得多**。
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", entry.title or entry.raw or ""))
        if has_cjk:
            msg = (
                f"参考文献 {where}「{label}」未能在 Crossref 与 OpenAlex 中确认存在。"
                "**这不等于该文献是编造的**：这两个库对中文期刊的收录很不完整，"
                "中文文献查不到是常态。请用知网/万方核实它是否真实存在。"
            )
            if note:
                msg += f"（{note}）"
            return Issue(
                id="",
                kind="citation.unconfirmed_cn",
                severity=Severity.MAJOR,
                message=msg,
                location={"ref": entry.index},
                evidence=entry.raw,
                detected_by="external:crossref+openalex",
                fixable=False,
                fix_hint=(
                    "在知网/万方检索该条目的题目与作者。若确实存在，本条可忽略；"
                    "若查不到，请核实是否为自己或 AI 生成的条目。补上 DOI 可提高复核可靠性。"
                ),
            )

        if quality == "high":
            msg = f"参考文献 {where}「{label}」在 Crossref 与 OpenAlex 均未找到，疑似编造的引用。"
            if note:
                msg += f"（{note}）"
            return Issue(
                id="",
                kind="citation.not_found",
                severity=Severity.BLOCKER,
                message=msg,
                location={"ref": entry.index},
                evidence=entry.raw,
                detected_by="external:crossref+openalex",
                fixable=False,
                fix_hint="请核实该文献是否真实存在，或从参考文献中删除。若确信存在，请补充 DOI 以便复核。",
            )
        # 标题质量可疑：抽取可能有偏差，**不能断言造假**。
        msg = (
            f"参考文献 {where}「{label}」在 Crossref 与 OpenAlex 均未找到，"
            f"但该条目的标题抽取可信度较低（{quality}，原文：{_snippet(entry.raw, 90)}），"
            "因此无法断定是否真实存在，请人工核对条目信息是否完整、准确。"
        )
        if note:
            msg += f"（{note}）"
        return Issue(
            id="",
            kind="citation.weak_match",
            severity=Severity.MAJOR,
            message=msg,
            location={"ref": entry.index},
            evidence=entry.raw,
            detected_by="external:crossref+openalex",
            fixable=False,
            fix_hint="核对题目是否被截断或混入了作者/刊名；补全题目或 DOI 后重跑外部核验。",
        )

    return None


class ExternalVerifyAgent(Agent):
    """把参考文献逐条送去真实数据库核验，产出 citation.* 类问题。"""

    name = "verify:external"
    description = (
        "把稿件里的参考文献逐条送往 Crossref / OpenAlex 核验真实性："
        "确认存在的静默通过，查不到的报为疑似编造（BLOCKER），"
        "只匹配上相似文献的转人工确认，查询失败的单独记为无法核验。"
        "不调用大模型——结论必须来自真实数据库。"
    )
    stage = Stage.VERIFY.value
    capabilities = ("verify", "external", "verify:references")
    uses_model = False

    def __init__(
        self,
        *,
        verifier_factory: Callable[[Any], ExternalVerifier] | None = None,
        max_cached: bool = True,
    ) -> None:
        """``verifier_factory`` 是给测试用的注入点。

        网络不该出现在单测里：测试要验证的是"结论 → 严重度"的映射与断点续验的
        行为，这些跟 Crossref 是否可达毫无关系。注入一个返回预定结论的假核验器，
        测试就变成确定性的，也不会因为断网而红。
        """
        self._factory = verifier_factory or make_external_verifier
        self._use_cache = max_cached

    # -- 主流程 -----------------------------------------------------------
    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        settings = ctx.settings
        if not getattr(settings.verify, "external_enabled", True):
            return self.report(
                ctx,
                ok=True,
                summary="外部核验已关闭（verify.external_enabled=False），跳过。",
                data={"skipped": True, "reason": "external_disabled"},
                seconds=time.time() - t0,
            )

        text = ctx.board.draft_text or ""
        _, ref_text = split_reference_section(text)
        if not ref_text.strip():
            # 草稿没有文献表是**正常情况**，不是错误。
            return self.report(
                ctx,
                ok=True,
                summary="未找到参考文献区，跳过外部核验。",
                data={"skipped": True, "reason": "no_reference_section"},
                seconds=time.time() - t0,
            )

        entries = parse_entries(ref_text)
        if not entries:
            return self.report(
                ctx,
                ok=True,
                summary="定位到参考文献区但未能切出条目，跳过外部核验。",
                data={"skipped": True, "reason": "no_entries"},
                seconds=time.time() - t0,
            )

        verifier = self._factory(settings)
        try:
            issues, data = self._verify_entries(ctx, entries, verifier)
        except Exception as exc:  # noqa: BLE001 - 核验整体失败也不能带崩流程
            return self.report(
                ctx,
                ok=False,
                summary="外部核验执行失败（已跳过，不影响其它检查）",
                error=f"{type(exc).__name__}: {exc}",
                data={"entries": len(entries)},
                seconds=time.time() - t0,
            )

        added = ctx.board.add_issues(issues, stage=ctx.stage, round_no=ctx.round) if issues else 0
        counts = data["counts"]
        done = data["checked"] + data["cached"]
        parts = [
            f"共 {data['entries']} 条",
            f"已核验 {done} 条",
            f"确认存在 {counts[VERDICT_CONFIRMED]} 条",
            f"疑似编造 {counts[VERDICT_NOT_FOUND]} 条",
            f"弱匹配 {counts[VERDICT_WEAK]} 条",
            f"无法核验 {counts[VERDICT_INCONCLUSIVE]} 条",
        ]
        if data["skipped_over_limit"]:
            parts.append(f"因超出单次上限（{data['limit']}）还有 {data['skipped_over_limit']} 条未核验")
        if data["cache_hits"]:
            parts.append(f"其中 {data['cache_hits']} 条命中上次核验结果")
        if added:
            parts.append(f"新增问题 {added} 条")

        return self.report(
            ctx,
            ok=True,
            summary="外部核验：" + "，".join(parts) + "。",
            issues=[i.id for i in issues],
            data=data,
            seconds=time.time() - t0,
        )

    # -- 逐条核验 ---------------------------------------------------------
    def _verify_entries(
        self, ctx: AgentContext, entries: list[RefEntry], verifier: ExternalVerifier
    ) -> tuple[list[Issue], dict]:
        limit = max(0, int(getattr(ctx.settings.verify, "max_external_lookups", 60)))
        delay = max(0.0, float(getattr(ctx.settings.verify, "external_delay", 0.2)))

        cached = load_cached(ctx.board) if self._use_cache else {}
        working = dict(cached)

        issues: list[Issue] = []
        counts = {VERDICT_CONFIRMED: 0, VERDICT_WEAK: 0, VERDICT_NOT_FOUND: 0, VERDICT_INCONCLUSIVE: 0}
        checked = 0
        cache_hits = 0
        no_lookup = 0
        total = len(entries)

        for i, entry in enumerate(entries, start=1):
            hit = cached.get(entry.fingerprint)
            if _skip_cached(hit):
                # 上次已有定论：直接用，不再发请求。
                result = {"verdict": hit.get("verdict"), "best": hit.get("best") or {}, "errors": []}
                cache_hits += 1
            elif checked >= limit:
                continue
            else:
                if not entry.lookupable:
                    no_lookup += 1
                result = self._verify_one(entry, verifier, no_lookup_field=not entry.lookupable)
                ok_to_cache = _skip_cached(result)
                if self._use_cache and ok_to_cache:
                    working[entry.fingerprint] = {
                        "fingerprint": entry.fingerprint,
                        "index": entry.index,
                        "verdict": result.get("verdict"),
                        "best": result.get("best") or {},
                        "checked_at": time.time(),
                    }
                    # 查一条写一次：跑到一半断网时，已经花掉的请求不会白费。
                    _store_cache(ctx.board, working)
                checked += 1
                if delay:
                    time.sleep(delay)

            counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
            issue = verdict_to_issue(entry, result)
            if issue is not None:
                issues.append(issue)
            self._report_progress(ctx, i, total)

        data = {
            "entries": total,
            "checked": checked,
            "cached": cache_hits,
            "cache_hits": cache_hits,
            "no_lookup_field": no_lookup,
            "limit": limit,
            "skipped_over_limit": total - checked - cache_hits,
            "counts": counts,
            "seconds": 0.0,
        }
        return issues, data

    @staticmethod
    def _verify_one(entry: RefEntry, verifier: ExternalVerifier, *, no_lookup_field: bool) -> dict:
        """核验单条。**任何异常都只记为该条无法核验，绝不上抛。**

        ``ExternalVerifier.verify()`` 内部已经把 ``PeerError`` 转成
        ``inconclusive`` 了，但补充一层兜底仍然必要：适配器之外的代码（正则、
        属性访问、第三方库）也可能抛异常，而一条条目把整个验证阶段带崩，等于
        60 条里剩下 59 条都没核验——**一次编码疏忽会让所有编造文献逃过检查**。
        """
        try:
            target = _NoLookup() if no_lookup_field else verifier
            kwargs: dict[str, Any] = {}
            if entry.doi and not no_lookup_field:
                kwargs["doi"] = entry.doi
            title = entry.search_title.strip()
            if title and not no_lookup_field:
                kwargs["title"] = title
            if not kwargs:
                kwargs["title"] = ""
            return target.verify(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 单条失败绝不影响其它条目
            return {
                "verdict": VERDICT_INCONCLUSIVE,
                "best": WorkRecord(
                    found=False,
                    source="none",
                    query=entry.doi or entry.search_title,
                    note=f"核验时发生异常：{type(exc).__name__}: {exc}",
                ).as_dict(),
                "errors": [f"{type(exc).__name__}: {exc}"],
                "records": [],
            }

    @staticmethod
    def _report_progress(ctx: AgentContext, i: int, total: int) -> None:
        """上报进度。**回调本身出错不能影响核验**——UI 的问题不是数据的问题。"""
        if ctx.progress is None or not total:
            return
        try:
            ctx.progress(f"外部核验 {i}/{total}", i / total)
        except Exception:  # noqa: BLE001 - 进度上报是尽力而为
            pass


# ---------------------------------------------------------------------------
# 本地检查器：数字型断言的来源标注
# ---------------------------------------------------------------------------

CHECK_NAME = "citation_grounding"

#: 章节标题的通用形态（用于"每节最多报几条"的分组）。**任意**章/节标题都算，
#: 不限于方法小节——否则整篇没写小节标题的稿子会被当成"一个巨大章节"，
#: 上限失效，一次冒出十几条提示。
_ANY_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[一二三四五六七八九十百\d]+\s*[章节篇]"
    r"|[一二三四五六七八九十]+\s*[、.．]"
    r"|\d+(?:\.\d+){0,3}\s*[、.．]?"
    r")\s*\S"
)

#: 哪些小节里的数字属于"研究设定"而不是"结论断言"。命中这些标题的小节整节跳过。
_METHOD_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十\d]+\s*[章节]|[一二三四五六七八九十\d]+\s*[、.．]|\d+(?:\.\d+)*\s*)?\s*"
    r"(研究方法|研究设计|模型(?:设定|构建)?|变量(?:定义|测量|选取)|指标(?:体系|选取)|"
    r"数据(?:来源|说明|处理|采集)|样本(?:选择|说明|概况)|计量模型|实验设计|"
    r"问卷设计|公式(?:说明)?|符号说明|method|methodology|data|variables|model)\s*[:：]?\s*$",
    re.I,
)

#: 段内已有来源标注的形式。命中任意一条就认为这段"交代了出处"，不再提示。
_GROUNDING_PATTERNS = (
    re.compile(r"[\[［]\s*\d{1,3}\s*(?:[-–—,，]\s*\d{1,3}\s*)*[\]］]"),  # [12] / [3-5] / [3,4]
    re.compile(r"\[\s*[A-Za-z]{1,4}\s*[\]］]"),                          # 字母编号 [A]
    re.compile(r"(?:图|表|Figure|Fig\.?|Table)\s*\d", re.I),
    re.compile(r"(?:见|参见|详见|引自|转引|来源|数据来源|资料来源|根据|据)\s*[:：]?"),
    re.compile(r"(?:国家统计局|统计年鉴|中国统计年鉴|世界银行|Wind|CSMAR|国泰安|"
               r"中国知网|知网|万方|教育部|财政部|中国人民银行|工信部|艾瑞|麦肯锡|"
               r"World\s+Bank|IMF|OECD|WHO|Eurostat)", re.I),
    re.compile(r"(?:本文|本研究|课题组|笔者)\s*(?:调查|调研|测算|统计|整理|计算|访谈)"),
    re.compile(r"(?:问卷|访谈|实验|案例|数据库|数据集|原始数据)"),
)

#: 数值断言的形态。设计原则：**只认"看起来像结论"的量词**，不认裸数字。
#: 裸数字（``3 个维度``、``第 2 章``、``样本 500 份``）太容易误报：它们绝大多数是
#: 描述而非断言。宁可漏报，也不能让作者被一堆噪声提示训练成"忽略这个工具"。
_ASSERTION_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?\s*%"),                        # 23.5%
    re.compile(r"\d+(?:\.\d+)?\s*(?:个)?\s*百?分点"),          # 5 个百分点
    re.compile(r"(?:增长|增加|提高|提升|上升|扩大|翻|高出|下降|降低|减少|缩短|slower|faster)"
               r"[^。；;\n]{0,8}?\d+(?:\.\d+)?\s*倍"),         # 提升了 3 倍
    re.compile(r"\d+(?:\.\d+)?\s*倍"),                       # 3.2 倍
    re.compile(r"显著性水平[^。；;\n]{0,6}?\d"),               # 显著性水平 0.05
    re.compile(r"p\s*[<>=]\s*0?\.\d+", re.I),                # p < 0.05
    re.compile(r"[Rr]\s*2\s*[=＝]\s*0?\.\d+"),                # R2 = 0.83
)

#: 引用语境词。命中说明这句在**陈述别人的结论**，来源由被引文献承担。
_CITE_VERBS_RE = re.compile(
    r"(?:等|等人|学者|研究表明|研究显示|研究指出|研究发现|已有研究|文献|作者|"
    r"et\s+al\.?|found|reported|suggested|showed)\s*(?:\(|\（|发现|指出|认为|表明|显示)?"
)
#: 假设/情景语境词。命中说明这不是在断言事实。
_HYPOTHETICAL_RE = re.compile(r"(?:假设|假定|情景|情境|如果|若|设想|预计|目标|计划|例如|比如)")
#: 引号内引述：``……提升了"23.5%"``。引号里的是转述，不该按自己的断言检查。
_QUOTED_RE = re.compile(r"[“\"'][^”\"']{0,40}\d[^”\"']{0,40}[”\"']")

#: 每个章节最多报几条。上限存在的理由是**误报的杀伤力**：如果一次给出几十条
#: 提示，作者会连正确的那些一起忽略。少量高置信提示才有可能被认真对待。
_MAX_PER_SECTION = 5
#: 全篇上限。有节标题的稿子靠"每节 5 条"自然收敛；一份**完全没有节标题**的稿子
#: 会被当成一个巨大章节，所以还需要一个总量兜底。
_MAX_TOTAL = 15

#: 判定窗口：断言所在句 + 上下各一句。太窄会漏掉"数据来源：国家统计局（见引言）"
#: 这类隔句交代；太宽会把无关的引用当成依据。
_WINDOW = 1


def _is_reference_body(text: str, start: int, end: int) -> bool:
    """判断某段是否落在参考文献区内（文献条目本身不是"数据断言"）。"""
    _, ref_text = split_reference_section(text)
    if not ref_text:
        return False
    ref_start = len(text) - len(ref_text)
    return start >= ref_start <= end


def _split_paragraphs(text: str) -> list[tuple[int, int, str]]:
    """按空行切段落，返回 ``[(起始偏移, 结束偏移, 段落文本)]``。

    必须带偏移：Issue 的 ``location`` 要有段落号，``evidence`` 要能回指原文。
    """
    out: list[tuple[int, int, str]] = []
    pos = 0
    for block in re.split(r"\n\s*\n", text or ""):
        start = text.find(block, pos) if block else pos
        if start < 0:
            start = pos
        end = start + len(block)
        pos = end
        if block.strip():
            out.append((start, end, block))
    return out


def _split_sentences(block: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？!?；;])|\n", block) if s and s.strip()]


def _section_of(paragraphs: list[tuple[int, int, str]], index: int) -> tuple[int, str]:
    """向上找最近的章节标题，返回 ``(段落下标, 标题文本)``。

    段落文本本身以标题开头时优先用自己（``2.3 研究方法`` 与正文同段的情况）。
    找不到任何标题时返回 ``(-1, "")``——调用方据此把"无标题区域"当成一个整体。
    """
    for i in range(index, -1, -1):
        first = _split_sentences(paragraphs[i][2])[:1]
        head = (first[0] if first else paragraphs[i][2]).strip()
        if len(head) <= 60 and _ANY_HEADING_RE.match(head):
            return i, head
    return -1, ""


def _is_method_section(paragraphs: list[tuple[int, int, str]], index: int) -> bool:
    """本段是否落在"研究方法/变量/数据来源"一类的小节里。

    这类小节里的数字是**研究设定**（"采用五点量表""样本 1200 份""R²=0.83"），
    不是需要出处的结论断言。把它们报出来是典型的误报来源。
    """
    for i in range(index, -1, -1):
        first = _split_sentences(paragraphs[i][2])[:1]
        head = (first[0] if first else paragraphs[i][2]).strip()
        if len(head) <= 60 and _ANY_HEADING_RE.match(head):
            return bool(_METHOD_HEADING_RE.match(head))
    return False


def _assertion_tokens(text: str) -> list[str]:
    """抽出一段里所有"数值断言"的原样串（去重，保持出现顺序）。"""
    found: list[str] = []
    for pat in _ASSERTION_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0)
            if tok not in found:
                found.append(tok)
    return found


def _grounded(window: str) -> str:
    """窗口内是否已有来源标注。返回命中的那条模式说明，未命中返回空串。"""
    for pat in _GROUNDING_PATTERNS:
        if pat.search(window):
            return pat.pattern
    return ""


def citation_grounding_issues(text: str) -> list[Issue]:
    """纯本地、纯规则的"数字断言缺来源"检查。

    **这个检查器宁可漏报，也不能误报。** 理由是它的提示会直接送到作者面前做
    人工判断，而误报的代价不对称：一条假警报看起来和真警报一模一样，攒够几条
    之后作者就会养成"这个工具的提示不用看"的习惯——那时候真正严重的
    ``citation.not_found`` 也会被同一双眼睛跳过。

    所以每一层都往"少报"的方向取：

    * 只认带量词的数值（百分比、百分点、倍数、p 值、R²），不认裸数字；
    * 只认**没有** ``[n]``、图表引用、"数据来源"、权威机构名、本文调查等标注的段落；
    * 跳过"研究方法/变量/模型/数据来源"小节——那里的数字是设定，不是结论；
    * 跳过含引用动词（"'XX 等'研究表明"）的句子；
    * 跳过引号内的引述与假设/情景句；
    * 每节最多报 ``_MAX_PER_SECTION`` 条。

    命中的都是"整段没有任何来源痕迹、却给出精确百分比/倍数"的强断言——
    这类句子正是评审最容易追问"数据从哪来"的地方。
    """
    issues: list[Issue] = []
    if not (text or "").strip():
        return issues

    paragraphs = _split_paragraphs(text)
    per_section: dict[int, int] = {}
    total_reported = 0

    for pidx, (start, end, block) in enumerate(paragraphs):
        if _is_reference_body(text, start, end):
            continue
        if _is_method_section(paragraphs, pidx):
            continue  # 方法/数据小节：数字是研究设定，不是需要出处的断言
        sec_idx, _ = _section_of(paragraphs, pidx)

        sentences = _split_sentences(block)
        for sidx, sentence in enumerate(sentences):
            tokens = _assertion_tokens(sentence)
            if not tokens:
                continue
            if _HYPOTHETICAL_RE.search(sentence):
                continue
            if _CITE_VERBS_RE.search(sentence):
                continue
            if _QUOTED_RE.search(sentence):
                continue

            lo = max(0, sidx - _WINDOW)
            hi = min(len(sentences), sidx + _WINDOW + 1)
            window = "".join(sentences[lo:hi])
            if _grounded(window):
                continue

            if total_reported >= _MAX_TOTAL:
                return issues
            if per_section.get(sec_idx, 0) >= _MAX_PER_SECTION:
                continue
            per_section[sec_idx] = per_section.get(sec_idx, 0) + 1
            total_reported += 1

            shown = "、".join(tokens[:4])
            issues.append(
                Issue(
                    id="",
                    kind="citation.ungrounded_number",
                    severity=Severity.MAJOR,
                    message=(
                        f"数字型断言缺少来源标注：「{_snippet(sentence, 90)}」"
                        f"（出现 {shown}）。正文里的精确数值需要能指回来源"
                        "（图表、引文编号或数据来源说明）。"
                    ),
                    location={"paragraph": pidx, "find": _snippet(sentence, 60)},
                    evidence=sentence,
                    detected_by="rules:citation_grounding",
                    fixable=False,
                    fix_hint="补上数据来源（图表编号、[n] 引文或「数据来源：…」说明），或弱化表述去掉精确数值。",
                )
            )
    return issues


@verifier(
    CHECK_NAME,
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description=(
        "本地规则检查：正文里带百分号/倍数/显著性水平的数字型断言是否有来源标注"
        "（图表、[n] 引文或数据来源说明）。刻意保守，只报完全没有来源痕迹的强断言。"
    ),
)
def check_citation_grounding(text: str, ctx: AgentContext | None = None) -> list[Issue]:  # noqa: ARG001
    """注册进 ``framework.registry`` 的本地检查器入口。

    签名与其它检查器一致（``text, ctx``），由 ``VerifierAgent`` 调用；
    ``ctx`` 在这里用不到——这个检查纯靠规则，不碰设置也不联网，这正是它能
    离线跑、能被单测的原因。
    """
    return citation_grounding_issues(text)
