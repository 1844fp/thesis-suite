"""③ 论文写作 agent。

能力：从 Word/PDF 文档导入写作计划、逐章生成初稿、单独重写某一章、
按评估意见定点改写、导出 Markdown / Word。
"""

from __future__ import annotations

import json

from .base import Agent, Context
from .bridge import make_file_request, make_request


class WriterAgent(Agent):
    name = "writer"
    title = "论文写作"
    summary = "从 Word/PDF 导入写作要求与正文，逐章写初稿，可单独重写某一章、按意见定点改写，导出 Word。"

    def __init__(self) -> None:
        self.actions = {
            "import_plan": {"handler": self.import_plan, "needs_key": False,
                            "desc": "上传 Word/PDF（base64），解析出可编辑的写作计划，正文同时存入依据"},
            "draft": {"handler": self.draft, "needs_key": True,
                      "desc": "逐章生成初稿（只依据证据与真实文献，数字与引用会被校验）"},
            "section": {"handler": self.section, "needs_key": True,
                        "desc": "只重写某一章（可附本次的额外要求；同样会校验引用与数字）"},
            "revise": {"handler": self.revise, "needs_key": True,
                       "desc": "按评估意见定点改写指定章节，未涉及问题的句子保持原样"},
            "export_markdown": {"handler": self.export_markdown, "needs_key": False,
                                "desc": "导出最新稿件为 Markdown"},
            "export_docx": {"handler": self.export_docx, "needs_key": False,
                            "desc": "导出最新稿件为 Word (.docx)"},
        }

    async def import_plan(self, ctx: Context, params: dict, creds) -> dict:
        """从上传的文档里读出写作计划。

        文件以 base64 传进来（JSON 里没法带二进制），这里解码后**直接复用
        ``/api/writing/import-plan`` 的 handler**——解析、兜底计划、材料块入库
        这几件事都只有一份实现，agent 这层只负责把二进制还原成 multipart 请求。
        """
        import base64
        import binascii

        from ..api import writing as writing_api

        raw_files = params.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            return {"ok": False, "error": "请提供至少一个文件（files 为 [{filename, content_base64}]）。"}

        files: list[tuple[str, str, bytes, str]] = []
        for i, item in enumerate(raw_files, start=1):
            if not isinstance(item, dict):
                return {"ok": False, "error": f"第 {i} 个文件格式不对：应为对象。"}
            name = str(item.get("filename") or f"文件{i}").strip() or f"文件{i}"
            try:
                data = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
            except (binascii.Error, ValueError):
                return {"ok": False, "error": f"「{name}」的 content_base64 不是合法的 base64。"}
            if not data:
                return {"ok": False, "error": f"「{name}」内容为空。"}
            files.append(("files", name, data,
                          str(item.get("content_type") or "application/octet-stream")))

        full = "1" if params.get("full") else "0"
        ev = ctx.begin(self.name, "import_plan", f"解析 {len(files)} 个文件并生成写作计划")
        try:
            # sid 走查询参数：handler 从 query 里取它会话，与 HTTP 路径完全一致。
            req = make_file_request(files, creds=creds,
                                    query={"sid": ctx.session.sid, "full": full})
            result = await writing_api.import_plan(req)
            plan = result.get("plan") or {}
            ctx.finish(ev, f"计划：{plan.get('title') or '（无题名）'}，"
                           f"{len(plan.get('sections') or [])} 章",
                       sections=len(plan.get("sections") or []),
                       material=result.get("material") or {})
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001 - handler 抛的 HTTPException 在这里也是普通异常
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def section(self, ctx: Context, params: dict, creds) -> dict:
        """只重写某一章。与整篇生成共用同一套材料表与校验，所以约束不会少一条。"""
        from ..api import writing as writing_api

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "重写单章需要你自己的 API Key。"}

        draft = ctx.session.draft
        if not draft:
            return {"ok": False, "error": "还没有稿件可供重写，请先在「写初稿」阶段生成。"}
        if not str(params.get("heading") or "").strip() and not isinstance(params.get("index"), int):
            return {"ok": False, "error": "请给出要重写的章节（heading 或 index）。"}

        body = {
            "sid": ctx.session.sid,
            "title": params.get("title") or ctx.session.meta.get("writing_title") or "",
            "heading": params.get("heading") or "",
            "index": params.get("index"),
            "task": params.get("task") or params.get("instructions") or "",
            "requirements": params.get("requirements") or "",
            "use_web": params.get("use_web", True),
        }
        heading = str(body["heading"] or "")
        ev = ctx.begin(self.name, "section",
                       f"重写章节：{heading or ('第 ' + str(body['index']) + ' 章')}")
        try:
            req = make_request(body, creds=creds)
            resp = await writing_api.section(req)
            version = None
            coverage = None
            errors: list[str] = []
            async for chunk in resp.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                for line in text.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if event.get("type") == "result":
                        version = event.get("draft")
                        coverage = event.get("coverage")
                    elif event.get("type") == "error":
                        errors.append(str(event.get("message") or ""))

            if version is None:
                msg = errors[0] if errors else "重写没有返回结果"
                ctx.fail(ev, msg)
                return {"ok": False, "error": msg}
            ctx.finish(ev, f"已重写「{version.get('rewritten')}」", coverage=coverage)
            return {"ok": True, "draft": version, "coverage": coverage,
                    "rewritten": version.get("rewritten"),
                    "version_count": len(ctx.session.versions)}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def draft(self, ctx: Context, params: dict, creds) -> dict:
        """逐章生成初稿。复用既有 SSE 路由，把事件流收集成结果。"""
        from ..api import writing as writing_api

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "写作需要你自己的 API Key。"}

        sections = params.get("sections") or []
        # 没给章节时，优先用上一步生成的大纲——这正是"阶段间数据流动"的价值：
        # 作者在大纲阶段确认过的结构，不该在写作阶段再手输一遍。
        if not sections:
            outline = ctx.get("outline") or ctx.session.meta.get("outline") or {}
            sections = [s.get("title", "") for s in outline.get("sections") or []]
        # 再退一步：如果刚用 Word/PDF 导过写作计划，就用那份结构。
        if not sections:
            plan = ctx.session.meta.get("document_plan") or {}
            sections = [str(s) for s in (plan.get("sections") or []) if str(s).strip()]

        body = {
            "sid": ctx.session.sid,
            "title": params.get("title") or ctx.session.meta.get("writing_title") or "",
            "genre": params.get("genre") or "",
            "sections": sections,
            "use_web": params.get("use_web", True),
            "requirements": params.get("requirements") or "",
        }
        if not str(body["title"]).strip():
            return {"ok": False, "error": "请先给出论文题目。"}

        ev = ctx.begin(self.name, "draft", f"逐章写作（{len(sections)} 章）")
        try:
            req = make_request(body, creds=creds)
            # evidence_internal 以关键字传入：这是给进程内调用方留的口子，
            # 见 writing.draft 的签名说明。
            resp = await writing_api.draft(req, evidence_internal=list(ctx.session.evidence))
            draft = None
            coverage = None
            errors: list[str] = []
            async for chunk in resp.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                for line in text.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if event.get("type") == "result":
                        draft = event.get("draft")
                        coverage = event.get("coverage")
                    elif event.get("type") == "error":
                        errors.append(str(event.get("message") or ""))

            if draft is None:
                msg = errors[0] if errors else "写作没有返回稿件"
                ctx.fail(ev, msg)
                return {"ok": False, "error": msg}

            ctx.session.meta["writing_title"] = body["title"]
            ctx.finish(ev, f"初稿完成：{draft.get('words', 0)} 字",
                       words=draft.get("words"), coverage=coverage)
            return {"ok": True, "draft": draft, "coverage": coverage}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def revise(self, ctx: Context, params: dict, creds) -> dict:
        """按意见定点改写某一章。

        只改被指出问题的句子：整段重写会让作者无法核对"改了什么"，
        而且很容易把原本有依据的句子改丢。所以提示词里把这条件写成硬规则，
        并且在返回值里区分"已解决"与"材料不足无法解决"。
        """
        from .. import capabilities as cap
        from .. import llm as llm_mod

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "按意见改写需要你自己的 API Key。"}

        draft = ctx.session.draft
        if not draft:
            return {"ok": False, "error": "还没有稿件可供修改，请先在「写初稿」阶段生成。"}

        heading = str(params.get("heading") or "").strip()
        target = None
        for s in draft.get("sections") or []:
            if heading and heading in str(s.get("heading") or ""):
                target = s
                break
        if target is None:
            return {"ok": False,
                    "error": f"没有找到章节「{heading}」。可用的章节："
                             + "、".join(str(s.get("heading")) for s in draft.get("sections") or [])}

        issues = params.get("issues")
        if not issues:
            # 没显式给意见时，用这一章在最近一次评估里的问题
            issues = [i for i in (ctx.session.meta.get("last_issues") or [])
                      if str(i.get("heading") or "") in str(target.get("heading") or "")]

        # 材料表：与写作阶段同源，保证改写后仍然"有据可查"
        ledger = ctx.session.meta.get("writing_evidence") or {}
        entries = [e for v in ledger.values() for e in v]
        block = "\n".join(
            f"[{i}] {e.get('citation') or ''}\n    原文：{str(e.get('text') or '')[:800]}"
            for i, e in enumerate(entries[:12], start=1)
        )

        ev = ctx.begin(self.name, "revise", f"改写：{target.get('heading')}")
        try:
            messages = cap.revision_prompt(str(target.get("heading") or ""),
                                           str(target.get("body") or ""), issues or [], block)
            with llm_mod.Chat(creds, timeout=240.0) as chat:
                raw = chat.ask_json(messages, temperature=0.3, max_tokens=6000)

            new_body = str((raw or {}).get("body") or "").strip()
            if not new_body:
                ctx.fail(ev, "模型没有返回修改后的正文")
                return {"ok": False, "error": "模型没有返回修改后的正文，请重试或换模型。"}

            # 生成新版本（保留旧版本可回退）——写作是会产生新版本的动作。
            import time as _t

            sections = [dict(s) for s in (draft.get("sections") or [])]
            for s in sections:
                if s is target or s.get("heading") == target.get("heading"):
                    s["body"] = new_body
                    s["words"] = len([c for c in new_body if "\u4e00" <= c <= "\u9fff"])
                    s["revised"] = True
            version = {
                "title": draft.get("title"),
                "genre": draft.get("genre"),
                "sections": sections,
                "citation_count": draft.get("citation_count"),
                "coverage": draft.get("coverage"),
                "words": sum(int(s.get("words") or 0) for s in sections),
                "notice": f"按意见改写了「{target.get('heading')}」",
                "revised_from": target.get("heading"),
                "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            }
            ctx.session.push_version(version)

            changes = (raw or {}).get("changes") or []
            unsolved = (raw or {}).get("unsolved") or []
            ctx.finish(ev, f"改写完成，{len(changes)} 处改动，{len(unsolved)} 条无法解决",
                       changes=len(changes), unsolved=len(unsolved))
            return {"ok": True, "draft": version, "changes": changes,
                    "unsolved": unsolved, "version_count": len(ctx.session.versions)}
        except llm_mod.LLMError as exc:
            ctx.fail(ev, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def export_markdown(self, ctx: Context, params: dict, creds) -> dict:
        from ..docx_export import draft_to_markdown

        draft = ctx.session.draft
        if not draft:
            return {"ok": False, "error": "还没有稿件可导出。"}
        return {"ok": True, "text": draft_to_markdown(draft),
                "filename": f"{draft.get('title') or '稿件'}.md"}

    async def export_docx(self, ctx: Context, params: dict, creds) -> dict:
        """导出 Word。返回 base64 —— 二进制没法走 JSON，但接口要保持 JSON 一致。"""
        import base64

        from ..docx_export import draft_to_markdown, markdown_to_docx

        draft = params.get("draft") or ctx.session.draft
        if not isinstance(draft, dict) or not draft.get("sections"):
            return {"ok": False, "error": "还没有稿件可导出。"}

        ev = ctx.begin(self.name, "export_docx", "导出 Word")
        try:
            md = draft_to_markdown(draft)
            data = markdown_to_docx(md, title=str(draft.get("title") or ""))
            ctx.finish(ev, f"生成 {len(data) // 1024} KB 的 .docx")
            return {
                "ok": True,
                "filename": f"{draft.get('title') or '稿件'}.docx",
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
            }
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
