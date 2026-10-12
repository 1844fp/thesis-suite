"""① 题目生成 agent。

能力：按学科方向生成候选题目（离线也能用），以及由题目生成章节大纲。
"""

from __future__ import annotations

import json

from .base import Agent, Context
from .bridge import make_request


class TopicAgent(Agent):
    name = "topic"
    title = "题目生成"
    summary = "按学科方向生成可落地的候选题目，并可由题目产出章节大纲。"

    def __init__(self) -> None:
        self.actions = {
            "generate": {"handler": self.generate, "needs_key": False,
                         "desc": "按学科方向生成候选题目（不填 Key 时走离线知识库）"},
            "outline": {"handler": self.outline, "needs_key": True,
                        "desc": "由题目与已有依据生成章节大纲（含每节要点与所需材料）"},
        }

    async def generate(self, ctx: Context, params: dict, creds) -> dict:
        """生成题目。复用既有 SSE 路由，把事件流收集成结果。

        为什么复用 SSE handler 而不是另写一个同步版本：生成过程是流式的（分批、
        截断补救、简报补齐），同步版等于把那条链路再实现一遍。
        这里把 SSE 帧收集起来，对调用方呈现为一个普通返回值。
        """
        from ..api import topics as topics_api

        req = make_request(params, creds=creds)
        ev = ctx.begin(self.name, "generate",
                       f"生成 {params.get('count', 6)} 个题目（{params.get('discipline', '')}）")
        try:
            resp = await topics_api.generate(req)
            result = None
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
                        result = event["result"]
                    elif event.get("type") == "error":
                        errors.append(str(event.get("message") or ""))

            if result is None:
                msg = errors[0] if errors else "生成没有返回结果"
                ctx.fail(ev, msg)
                return {"ok": False, "error": msg}

            # 把选题目录也放进黑板与状态：后续阶段要拿它当章节骨架的参考。
            ctx.put("topic_result", result)
            ctx.session.meta["discipline"] = result.get("discipline", "")
            ctx.finish(ev, f"生成 {result.get('count', 0)} 个题目",
                       count=result.get("count", 0), mode=result.get("mode"))
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - agent 不该把异常漏给调用方
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def outline(self, ctx: Context, params: dict, creds) -> dict:
        """生成章节大纲。需要模型。"""
        from .. import capabilities as cap
        from .. import llm as llm_mod

        title = str(params.get("title") or ctx.session.meta.get("writing_title") or "").strip()
        if not title:
            return {"ok": False, "error": "请先给出论文题目。"}
        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "生成大纲需要你自己的 API Key。"}

        genre_name = str(params.get("genre_name") or params.get("genre") or "")
        section_titles = [str(s) for s in (params.get("sections") or []) if str(s).strip()]
        evidence_titles = [
            str(e.get("title") or e.get("citation") or "")
            for e in ctx.session.evidence if e.get("title") or e.get("citation")
        ]

        ev = ctx.begin(self.name, "outline", f"生成大纲：{title[:30]}")
        try:
            messages = cap.outline_prompt(title, genre_name, evidence_titles, section_titles)
            with llm_mod.Chat(creds, timeout=180.0) as chat:
                raw = chat.ask_json(messages, temperature=0.3, max_tokens=4000)
            outline = cap.parse_outline(raw)
            if not outline.get("sections"):
                ctx.fail(ev, "模型没有返回可用的大纲结构")
                return {"ok": False, "error": "模型没有返回可用的大纲结构，请换一个模型重试。"}

            ctx.put("outline", outline)
            ctx.session.meta["outline"] = outline
            n = len(outline["sections"])
            ctx.finish(ev, f"生成 {n} 章大纲", sections=n)
            return {
                "ok": True,
                "outline": outline,
                # 直接给出可粘进写作阶段章节框的文本，省掉一次手工整理
                "section_text": cap.outline_to_text(outline),
            }
        except llm_mod.LLMError as exc:
            ctx.fail(ev, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            ctx.fail(ev, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
