"""⑤ 查重与降 AIGC 率 agent。

这个板块其实是**两件不同的事**，放在一起是因为作者在同一个阶段做它们：

* **查重** —— 找出同一份稿子里高度雷同的段落，以及参考文献能否在公开库里对上；
* **降 AIGC 率** —— 用本地风格指标体检，再分段改写，改写后做保真校验与复检。

两件事都有同一个**必须说清的边界**：它们都不是学校用的那套系统的输出。
查重只做文档内部自查，AIGC 指数是本地风格启发式代理。
这两句会出现在每次返回里，而不是只写在文档里——用户只看结果。
"""

from __future__ import annotations

from .base import Agent, Context


class IntegrityAgent(Agent):
    name = "integrity"
    title = "查重与降 AIGC 率"
    summary = "文档内部重复自查 + 参考文献可核验性 + 本地 AI 味体检与保真改写。"

    def __init__(self) -> None:
        self.actions = {
            "duplication": {"handler": self.duplication, "needs_key": False,
                            "desc": "查重风险自查：文档内部重复 + 参考文献可核验性，不需要 Key"},
            "aigc_score": {"handler": self.aigc_score, "needs_key": False,
                           "desc": "AI 味体检：11 项风格指标 + 29 条规则，不需要 Key"},
            "aigc_reduce": {"handler": self.aigc_reduce, "needs_key": False,
                            "desc": "分段改写降低 AI 味（默认关闭：费用记在站主 Key 上，"
                                    "站主设 SUITE_ALLOW_AIGC_REDUCE=1 才开）"},
            "aigc_export_docx": {"handler": self.aigc_export_docx, "needs_key": False,
                                 "desc": "把改写后的文本导出为 Word"},
        }

    async def duplication(self, ctx: Context, params: dict, creds) -> dict:
        from .. import capabilities as cap

        text = params.get("text") or ""
        if not text.strip():
            draft = ctx.session.draft
            if draft:
                text = "\n".join(str(s.get("body") or "") for s in draft.get("sections") or [])
        if not text.strip():
            return {"ok": False, "error": "没有可自查的正文：请先生成初稿或粘贴稿件。"}

        ev = ctx.begin(self.name, "duplication", "查重风险自查")
        try:
            internal = cap.duplication_check(
                text, threshold=float(params.get("threshold") or cap.DUPLICATE_THRESHOLD))
            refs = cap.citation_reachability(ctx.session.evidence,
                                             limit=int(params.get("ref_limit") or 10))
            risk = internal["internal_risk"]
            if refs["unverifiable"]:
                risk = "high" if risk == "medium" else risk
            ctx.finish(ev, f"内部重复 {internal['duplicate_pair_count']} 处，"
                           f"查不到的文献 {len(refs['unverifiable'])} 条")
            return {
                "ok": True, "risk": risk, "internal": internal, "references": refs,
                "disclaimer": (
                    "**这不是学校查重**。只做两件本机能做的事：① 同一份稿子内部的重复；"
                    "② 参考文献能否在公开文献库里对上。**检测不了**与已发表文献、网络内容、"
                    "往届论文的重复。正式查重请用学校指定系统。"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def aigc_score(self, ctx: Context, params: dict, creds) -> dict:
        from .. import aigc

        text = params.get("text") or ""
        if not text.strip():
            draft = ctx.session.draft
            if draft:
                text = "\n".join(str(s.get("body") or "") for s in draft.get("sections") or [])
        if not text.strip():
            return {"ok": False, "error": "没有可体检的正文。"}

        ev = ctx.begin(self.name, "aigc_score", "AI 味体检")
        try:
            result = aigc.score(text, creds=creds)
            if result.get("ok") is False:
                ctx.fail(ev, str(result.get("error"))[:120])
                return result
            idx = result.get("index") or result.get("ai_index") or result.get("score")
            ctx.finish(ev, f"AI 味指数 {idx}")
            return result
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def aigc_reduce(self, ctx: Context, params: dict, creds) -> dict:
        """降 AIGC 率改写。

        **这一步的模型费用不由访客承担，所以默认关闭。**
        ``aigc-reducer`` 的 HTTP 接口不接受 base_url/api_key/model，它只读自己的
        ``.env``——也就是说走这条链路时调用记在**那个服务配置的 Key**上。
        实测那台服务的 ``.env`` 里确实配着站主的 Key。若默认开放，
        公网访客每点一次"改写"都在消耗站主的余额，这与本站的 BYOK 定位直接冲突。

        所以默认只开放**纯本地的** ``aigc_score``（体检不调模型、不花钱），
        改写要站主显式设置 ``SUITE_ALLOW_AIGC_REDUCE=1`` 才可用。
        这不是技术限制，是**计费归属**的决定，必须由站主来做——
        工具替站主决定"帮别人付钱"是不合适的。
        """
        import os

        from .. import aigc

        if os.environ.get("SUITE_ALLOW_AIGC_REDUCE", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            return {
                "ok": False,
                "error": (
                    "降 AIGC 改写当前未开放。"
                    "原因是这一步的模型调用费用记在**本站服务配置的 Key** 上，而不是你自己的 Key"
                    "（上游服务不支持转发访客凭据）。站主可用 SUITE_ALLOW_AIGC_REDUCE=1 开启。"
                ),
                "credentials_note": aigc.CREDENTIAL_POLICY,
                "disabled_reason": "billing_belongs_to_site_owner",
                "available_instead": ["aigc_score", "duplication"],
            }

        text = params.get("text") or ""
        if not text.strip():
            draft = ctx.session.draft
            if draft:
                text = "\n".join(str(s.get("body") or "") for s in draft.get("sections") or [])
        if not text.strip():
            return {"ok": False, "error": "没有可改写的正文。"}

        ev = ctx.begin(self.name, "aigc_reduce", "降 AIGC 率改写")
        try:
            opts = {k: v for k, v in params.items()
                    if k in ("strength", "max_rounds", "concurrency", "keep_structure")}
            result = aigc.reduce_text(text, creds=creds, **opts)
            if result.get("ok") is False:
                ctx.fail(ev, str(result.get("error"))[:120])
                return result
            new_text = result.get("text") or result.get("revised") or ""
            if new_text:
                ctx.put("aigc_revised", new_text)
                ctx.session.meta["aigc_revised"] = new_text
            before = result.get("before") or result.get("before_index")
            after = result.get("after") or result.get("after_index")
            ctx.finish(ev, f"改写完成：{before} → {after}")
            return result
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def aigc_export_docx(self, ctx: Context, params: dict, creds) -> dict:
        import base64

        from .. import aigc

        text = params.get("text") or ctx.get("aigc_revised") or ctx.session.meta.get("aigc_revised") or ""
        if not str(text).strip():
            return {"ok": False, "error": "没有可导出的文本：请先跑一次降 AIGC 改写。"}
        ev = ctx.begin(self.name, "aigc_export_docx", "导出改写稿 Word")
        try:
            data = aigc.export_docx(str(text), title=str(params.get("title") or "改写稿"))
            ctx.finish(ev, f"{len(data) // 1024} KB")
            return {
                "ok": True, "filename": f"{params.get('title') or '改写稿'}.docx",
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
            }
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
