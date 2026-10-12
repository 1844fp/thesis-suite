"""⑥ 答辩 agent：由稿件生成答辩稿与演示幻灯片。

两件产物对应两种场合，所以提示词与结构都不共用：

* **答辩稿** —— 口播用（陈述 8~10 分钟 + 预计提问与应答）。读出来要顺，
  所以是连续文字，不是要点列表。
* **幻灯片** —— 投屏用。每页 3~5 条要点、**每条不超过 25 字**。
  演示稿不是文档，长句子投到屏幕上没人读——这条约束写在提示词里，也在解析时校验。

一条硬纪律与写作阶段一致：**只能依据给定材料，不得编造数据**。
材料不足以支撑某一页时，允许模型标注"材料不足"而不是编——答辩现场被问住，
比幻灯片上少一页严重得多。
"""

from __future__ import annotations

import base64

from .base import Agent, Context


class DefenseAgent(Agent):
    name = "defense"
    title = "答辩"
    summary = "由稿件生成答辩稿（陈述 + 预计提问）与演示幻灯片（.pptx 或 HTML）。"

    def __init__(self) -> None:
        self.actions = {
            "script": {"handler": self.script, "needs_key": True,
                       "desc": "生成答辩稿：陈述稿 + 预计提问与应答"},
            "slides": {"handler": self.slides, "needs_key": True,
                       "desc": "生成演示幻灯片结构（每页标题 + 要点）"},
            "pptx": {"handler": self.pptx, "needs_key": False,
                     "desc": "把幻灯片结构导出为 .pptx"},
            "html": {"handler": self.html, "needs_key": False,
                     "desc": "把幻灯片结构导出为单文件 HTML 演示稿（可离线打开）"},
        }

    # -- 内部：取稿件文本与章节标题 ----------------------------------------
    def _draft(self, ctx: Context, params: dict) -> tuple[dict | None, str, list[str]]:
        draft = params.get("draft") or ctx.session.draft
        if not isinstance(draft, dict) or not draft.get("sections"):
            return None, "", []
        text = "\n\n".join(
            f"{s.get('heading') or ''}\n{s.get('body') or ''}" for s in draft["sections"]
        )
        titles = [str(s.get("heading") or "") for s in draft["sections"]]
        return draft, text, titles

    async def script(self, ctx: Context, params: dict, creds) -> dict:
        from .. import defense, llm as llm_mod

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "生成答辩稿需要你自己的 API Key。"}
        draft, text, titles = self._draft(ctx, params)
        if not draft:
            return {"ok": False, "error": "还没有稿件：请先在「写初稿」阶段生成。"}

        ev = ctx.begin(self.name, "script", "生成答辩稿")
        try:
            evidence_titles = [str(e.get("title") or e.get("citation") or "")
                               for e in ctx.session.evidence][:20]
            messages = defense.script_prompt(str(draft.get("title") or ""),
                                             str(draft.get("genre") or ""), text, evidence_titles)
            with llm_mod.Chat(creds, timeout=300.0) as chat:
                raw = chat.ask_json(messages, temperature=0.3, max_tokens=6000)
            script = defense.parse_script(raw)
            ctx.put("defense_script", script)
            ctx.session.meta["defense_script"] = script
            n_qa = len(script.get("qa") or [])
            ctx.finish(ev, f"答辩稿完成（陈述 {len(str(script.get('statement') or ''))} 字，"
                           f"{n_qa} 个预计提问）")
            return {"ok": True, "script": script,
                    "markdown": defense.script_to_markdown(script)}
        except llm_mod.LLMError as exc:
            ctx.fail(ev, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def slides(self, ctx: Context, params: dict, creds) -> dict:
        from .. import defense, llm as llm_mod

        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "生成幻灯片需要你自己的 API Key。"}
        draft, text, titles = self._draft(ctx, params)
        if not draft:
            return {"ok": False, "error": "还没有稿件：请先在「写初稿」阶段生成。"}

        ev = ctx.begin(self.name, "slides", "生成演示幻灯片")
        try:
            messages = defense.slides_prompt(str(draft.get("title") or ""), text, titles)
            with llm_mod.Chat(creds, timeout=300.0) as chat:
                raw = chat.ask_json(messages, temperature=0.3, max_tokens=4000)
            slides = defense.parse_slides(raw)
            ctx.put("defense_slides", slides)
            ctx.session.meta["defense_slides"] = slides
            n = len(slides.get("slides") or [])
            ctx.finish(ev, f"生成 {n} 页幻灯片")
            return {"ok": True, "slides": slides,
                    "pptx_available": defense.pptx_available()}
        except llm_mod.LLMError as exc:
            ctx.fail(ev, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _slides_of(self, ctx: Context, params: dict) -> dict | None:
        slides = params.get("slides") or ctx.get("defense_slides") or ctx.session.meta.get("defense_slides")
        if not isinstance(slides, dict) or not slides.get("slides"):
            return None
        return slides

    async def pptx(self, ctx: Context, params: dict, creds) -> dict:
        from .. import defense

        slides = self._slides_of(ctx, params)
        if not slides:
            return {"ok": False, "error": "还没有幻灯片结构：请先生成幻灯片。"}
        ev = ctx.begin(self.name, "pptx", "导出 .pptx")
        try:
            data = defense.build_pptx(slides, title=str(params.get("title") or ""))
            ctx.finish(ev, f"{len(data) // 1024} KB")
            return {
                "ok": True, "filename": f"{params.get('title') or '答辩演示'}.pptx",
                "content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data),
            }
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def html(self, ctx: Context, params: dict, creds) -> dict:
        from .. import defense

        slides = self._slides_of(ctx, params)
        if not slides:
            return {"ok": False, "error": "还没有幻灯片结构：请先生成幻灯片。"}
        ev = ctx.begin(self.name, "html", "导出 HTML 演示稿")
        try:
            html = defense.build_html_deck(slides, title=str(params.get("title") or ""))
            ctx.finish(ev, f"{len(html) // 1024} KB")
            return {"ok": True, "filename": f"{params.get('title') or '答辩演示'}.html",
                    "content_type": "text/html; charset=utf-8", "html": html,
                    "bytes": len(html.encode("utf-8"))}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
