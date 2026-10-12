"""④ 评估 agent。

能力：跑 53 个本地检查器、核验参考文献、生成审稿意见书、查重风险自查、学校模板比对。
"""

from __future__ import annotations

from .base import Agent, Context
from .bridge import make_file_request, make_request


class ReviewAgent(Agent):
    name = "review"
    title = "评估"
    summary = "53 个本地检查器 + 文献核验 + 审稿意见书 + 查重风险自查 + 学校模板比对。"

    def __init__(self) -> None:
        self.actions = {
            "check": {"handler": self.check, "needs_key": False,
                      "desc": "跑全部本地检查器（引用编号、数字依据、写作质量…），不需要 Key"},
            "verify_refs": {"handler": self.verify_refs, "needs_key": False,
                            "desc": "核验稿件里参考文献的真伪，走 Crossref，不需要 Key"},
            "opinion": {"handler": self.opinion, "needs_key": True,
                        "desc": "在检查结果之上生成审稿意见书（评分由检查结果推导，模型不改分）"},
            "duplication": {"handler": self.duplication, "needs_key": False,
                            "desc": "查重风险自查：文档内部重复 + 参考文献可核验性"},
            "template": {"handler": self.template, "needs_key": False,
                         "desc": "学校模板比对：识别模板格式并列出稿件不合规项"},
        }

    def _text(self, ctx: Context, params: dict) -> str:
        """取待检文本：显式给的优先，否则用会话里的最新稿件。"""
        from ..api.review import _text_of

        return _text_of({"text": params.get("text") or ""}, ctx.session.sid)

    async def check(self, ctx: Context, params: dict, creds) -> dict:
        from ..api import review as review_api

        if not self._text(ctx, params).strip():
            return {"ok": False, "error": "没有可评估的正文：请先生成初稿或粘贴稿件。"}

        ev = ctx.begin(self.name, "check", "跑本地检查器")
        try:
            body = {"sid": ctx.session.sid, "text": params.get("text") or "",
                    "title": params.get("title") or ctx.session.meta.get("writing_title") or ""}
            req = make_request(body, creds=creds)
            data = await review_api.check(req)
            # 把逐条问题存进会话：写作 agent 的"按意见修改"要用它
            ctx.session.meta["last_issues"] = [
                {"kind": f.get("kind"), "message": f.get("message"),
                 "fix_hint": f.get("fix_hint"), "evidence": f.get("evidence"),
                 "dimension": dim}
                for dim, items in (data.get("dimensions") or {}).items()
                for f in items
            ]
            ctx.finish(ev, f"{data.get('issue_count', 0)} 个问题，评分 {data.get('score')}",
                       issues=data.get("issue_count"), score=data.get("score"))
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def verify_refs(self, ctx: Context, params: dict, creds) -> dict:
        from ..api import review as review_api

        ev = ctx.begin(self.name, "verify_refs", "核验参考文献真伪")
        try:
            body = {"sid": ctx.session.sid, "text": params.get("text") or "",
                    "limit": params.get("limit", 15)}
            req = make_request(body, creds=creds)
            data = await review_api.verify_references(req)
            ctx.finish(ev, f"核验 {data.get('checked', 0)} 条：{data.get('tally')}",
                       tally=data.get("tally"))
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def opinion(self, ctx: Context, params: dict, creds) -> dict:
        from ..api import review as review_api

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "生成审稿意见书需要你自己的 API Key。"}
        ev = ctx.begin(self.name, "opinion", "生成审稿意见书")
        try:
            body = {"sid": ctx.session.sid, "text": params.get("text") or "",
                    "title": params.get("title") or ctx.session.meta.get("writing_title") or ""}
            req = make_request(body, creds=creds)
            data = await review_api.opinion(req)
            ctx.finish(ev, f"意见书完成（评分 {data.get('score')}）", score=data.get("score"))
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def duplication(self, ctx: Context, params: dict, creds) -> dict:
        """查重风险自查。**不需要 Key**，但**不能替代学校查重**。"""
        from .. import capabilities as cap

        text = self._text(ctx, params)
        if not text.strip():
            return {"ok": False, "error": "没有可自查的正文。"}

        ev = ctx.begin(self.name, "duplication", "查重风险自查")
        try:
            internal = cap.duplication_check(text, threshold=float(params.get("threshold") or cap.DUPLICATE_THRESHOLD))
            refs = cap.citation_reachability(ctx.session.evidence,
                                             limit=int(params.get("ref_limit") or 10))
            risk = internal["internal_risk"]
            if refs["unverifiable"]:
                risk = "high" if risk == "medium" else risk
            ctx.finish(ev, f"内部重复 {internal['duplicate_pair_count']} 处，"
                           f"查不到的文献 {len(refs['unverifiable'])} 条",
                       internal=internal["duplicate_pair_count"],
                       unverifiable=len(refs["unverifiable"]))
            return {
                "ok": True,
                "risk": risk,
                "internal": internal,
                "references": refs,
                # 这句必须出现在返回里，不能只写在文档里——它是这个功能的安全边界
                "disclaimer": (
                    "**这不是学校查重**。本功能只做两件本机能做的事：① 同一份稿子内部的重复；"
                    "② 参考文献能否在公开文献库里对上。它**检测不了**与已发表文献、网络内容、"
                    "往届论文的重复。正式查重请用学校指定系统。"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def template(self, ctx: Context, params: dict, creds) -> dict:
        """学校模板比对。``params['template']`` / ``['manuscript']`` 是文件字节。"""
        from .. import capabilities as cap

        tpl = params.get("template")
        man = params.get("manuscript")
        if not tpl:
            return {"ok": False, "error": "请上传学校模板（.docx）。"}
        if not man:
            # 没给稿件时，从会话里现造一个 .docx 来比——作者通常刚导过稿
            draft = ctx.session.draft
            if not draft:
                return {"ok": False, "error": "请上传稿件，或先生成一版初稿。"}
            from ..docx_export import draft_to_markdown, markdown_to_docx

            man = markdown_to_docx(draft_to_markdown(draft), title=str(draft.get("title") or ""))

        def _as_bytes(v) -> bytes:
            return v.encode("utf-8") if isinstance(v, str) else bytes(v)

        ev = ctx.begin(self.name, "template", "学校模板比对")
        try:
            result = cap.compare_with_template(_as_bytes(man), _as_bytes(tpl))
            if result.get("error"):
                ctx.fail(ev, result["error"])
                return {"ok": False, "error": result["error"]}
            ctx.finish(ev, f"发现 {len(result['diffs'])} 处不合规",
                       diffs=len(result["diffs"]), conform=result["conform"])
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
