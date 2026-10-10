"""选题生成流水线：联网（大模型）与离线（内置知识库）两种模式。

对上层只暴露一个生成器 ``generate_stream``，依次吐出进度事件，
最后一个事件类型为 ``result``。Web 端据此做 SSE 推送，CLI 端直接消费。
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Iterator

from . import offline, prompts, validate
from .config import Settings, load_settings
from .llm import LLMClient, LLMError, extract_json


class GenerationError(RuntimeError):
    """生成失败，消息可直接展示给用户。"""


def _event(kind: str, **payload) -> dict:
    return {"type": kind, **payload}


def _setup_fraction(done: int, target: int) -> float:
    return min(0.92, 0.15 + 0.75 * (done / max(target, 1)))


def plan_batches(count: int) -> list[int]:
    """把题目数量切成若干批，避免单次输出被 max_tokens 截断。

    经验值：一个带完整简报的题目大约要 900~1400 tokens 输出。
    """
    count = max(1, int(count))
    batches: list[int] = []
    remaining = count
    while remaining > 0:
        size = 6 if remaining > 8 else remaining
        batches.append(size)
        remaining -= size
    return batches


def stream_chat_iter(client: LLMClient, messages: list, model_params: dict) -> Iterator[str]:
    """把阻塞式流式调用变成可在主线程消费的分片生成器。

    ``stream_chat`` 在后台线程里跑，分片经队列回到主线程依次 yield，
    这样 SSE 可以边收边推；出错时在流末尾把异常抛回主线程。
    """
    pieces: "queue.Queue[str | None]" = queue.Queue(maxsize=2048)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            client.stream_chat(messages, on_delta=lambda piece: pieces.put(piece), **model_params)
        except Exception as exc:  # noqa: BLE001 - 原样带回主线程
            errors.append(exc)
        finally:
            pieces.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while True:
        item = pieces.get()
        if item is None:
            break
        yield item
    thread.join(timeout=2)
    if errors:
        raise errors[0]


def generate_stream(
    settings: Settings | None = None,
    *,
    mode: str = "auto",
    client: LLMClient | None = None,
    include_raw: bool = True,
    **params,
) -> Iterator[dict]:
    """生成选题，逐事件产出。

    params 可用键：discipline, direction, level, topic_types, count, keywords,
    methodology, language, seed, extra, avoid, temperature, max_tokens
    """
    settings = settings or load_settings()
    count = max(1, min(int(params.get("count") or 6), 30))
    params["count"] = count
    mode = (mode or "auto").lower()
    avoid = [str(item) for item in (params.get("avoid") or []) if str(item).strip()]
    prompt_params = {key: value for key, value in params.items() if key not in ("avoid", "count")}

    # ---------------- 离线模式 ----------------
    if mode == "offline" or (mode == "auto" and not settings.ready):
        if mode == "llm":
            raise GenerationError("未配置 API Key，无法使用联网模式。")
        if mode == "auto":
            yield _event(
                "stage",
                stage="fallback",
                message="未检测到可用的 API Key，已自动切换到离线内置知识库模式。",
                progress=0.1,
            )
        else:
            yield _event("stage", stage="offline", message="使用离线内置知识库组合生成…", progress=0.1)
        offline_params = {key: value for key, value in params.items() if key != "avoid"}
        result = offline.generate(avoid=avoid, **offline_params)
        result["mode"] = "offline"
        result["level"] = params.get("level", "")
        yield _event("result", result=result)
        return

    if not settings.ready:
        raise GenerationError("未配置 API Key：请在网页右上角「设置」里填写，或改用离线模式。")

    # ---------------- 联网模式 ----------------
    client = client or LLMClient(settings)
    model_params = {
        "temperature": params.get("temperature", settings.temperature),
        "max_tokens": params.get("max_tokens", settings.max_tokens),
    }
    batches = plan_batches(count)
    yield _event(
        "stage",
        stage="plan",
        message=f"准备生成 {count} 个题目，分 {len(batches)} 批（{'/'.join(map(str, batches))}）",
        progress=0.05,
        batches=batches,
    )

    collected: list[dict] = []
    notices: list[str] = []
    avoid_now = list(avoid)
    pending: list[str] = []

    def drain_raw() -> Iterator[dict]:
        if include_raw and pending:
            text = "".join(pending)
            pending.clear()
            yield _event("delta", text=text)

    def run_batch(label: str, size: int, progress: float, batch_no: int | None) -> Iterator[dict]:
        """跑一批，把事件流出去，并把解析结果放进 collected。"""
        yield _event("stage", stage="llm", message=label, progress=progress)
        chunks: list[str] = []
        try:
            for piece in stream_chat_iter(client, prompts.build_messages(
                settings.system_prompt,
                prompts.build_user_prompt(avoid=avoid_now, count=size, **prompt_params),
            ), model_params):
                chunks.append(piece)
                if include_raw:
                    pending.append(piece)
                    if sum(len(item) for item in pending) > 200:
                        yield from drain_raw()
        except LLMError as exc:
            if collected:
                notices.append(f"第 {batch_no or '-'} 批中断：{exc}；已保留前 {len(collected)} 个题目。")
                yield _event("stage", stage="warn", message=str(exc), progress=progress)
                return
            raise GenerationError(str(exc)) from exc
        finally:
            if include_raw:
                yield from drain_raw()

        text = "".join(chunks)
        parsed = extract_json(text)
        if parsed is None:
            snippet = text.strip()[:160].replace("\n", " ")
            if collected:
                notices.append(f"有一批返回的内容无法解析，已跳过。原文片段：{snippet}")
                yield _event("stage", stage="warn", message="有一批返回内容无法解析，已跳过。", progress=progress)
                return
            raise GenerationError(
                "模型没有返回可解析的 JSON。原文片段：" + snippet
                + "。可尝试把温度降到 0.7 左右，或换一个更强的模型。"
            )

        batch = validate.normalize(parsed, discipline=params.get("discipline", ""), requested=size)
        if batch.get("notice"):
            notices.append(str(batch["notice"]))
        added = 0
        for topic in batch["topics"]:
            if topic["title"] in avoid_now or len(collected) >= count:
                continue
            topic["id"] = len(collected) + 1
            collected.append(topic)
            avoid_now.append(topic["title"])
            added += 1
        if batch_no:
            yield _event(
                "stage",
                stage="batch",
                message=f"第 {batch_no} 批完成：新增 {added} 个，累计 {len(collected)}/{count}",
                progress=_setup_fraction(len(collected), count),
                batch=batch_no,
                got=added,
            )

    for batch_index, size in enumerate(batches, 1):
        label = f"第 {batch_index}/{len(batches)} 批：正在生成 {size} 个题目"
        events: list[dict] = []
        error: GenerationError | None = None
        try:
            for event in run_batch(label, size, _setup_fraction(len(collected), count), batch_index):
                events.append(event)
        except GenerationError as exc:
            error = exc
        for event in events:
            yield event
        if error is not None:
            raise error
        if len(collected) >= count:
            break

    # 数量不足时补一轮（只补差额，最多一次）
    if 0 < len(collected) < count:
        missing = count - len(collected)
        yield _event("stage", stage="topup", message=f"数量不足，正在补生成 {missing} 个…", progress=0.93)
        events = []
        error = None
        try:
            for event in run_batch(f"补生成 {missing} 个题目", missing, 0.93, None):
                events.append(event)
        except GenerationError as exc:
            error = exc
        for event in events:
            yield event
        if error is not None:
            notices.append(str(error))

    if not collected:
        raise GenerationError("没有生成任何可用题目，请调低温度或更换模型后重试。")

    # 有的题目可能是被截断后救回来的（题目在、简报没写完）。
    # 题目本身是好的，不该整批重来 —— 只针对这几道补一次简报。
    missing_brief = [topic for topic in collected if not topic.get("brief_from_model")]
    if missing_brief:
        titles = "、".join(topic["title"][:20] for topic in missing_brief[:3])
        yield _event(
            "stage",
            stage="brief",
            message=f"有 {len(missing_brief)} 个题目缺简报（{titles}…），正在单独补齐…",
            progress=0.95,
        )
        events: list[dict] = []
        try:
            for event in complete_briefs(
                client, settings, collected, missing_brief, discipline=params.get("discipline", ""),
                include_raw=include_raw, model_params=model_params, pending=pending,
            ):
                events.append(event)
        except (LLMError, GenerationError) as exc:
            notices.append(f"有一批题目的简报没能补齐（{exc}），已用占位简报兜底。")
        for event in events:
            yield event

    result = {
        "discipline": params.get("discipline", ""),
        "direction": params.get("direction", ""),
        "level": params.get("level", ""),
        "seed": params.get("seed", ""),
        "requested": count,
        "count": len(collected),
        "topics": collected,
        "notice": " ".join(dict.fromkeys(item for item in notices if item)),
        "notice_kind": "warning" if len(collected) < count else "info",
        "mode": "llm",
        "model": settings.model,
        "provider": settings.provider or settings.base_url,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    yield _event("result", result=result)


def complete_briefs(
    client: LLMClient,
    settings: Settings,
    collected: list[dict],
    missing: list[dict],
    *,
    discipline: str = "",
    include_raw: bool = False,
    model_params: dict | None = None,
    pending: list[str] | None = None,
) -> Iterator[dict]:
    """只给「题目已定但简报缺失」的题目补简报，就地更新 collected。"""
    indices = [collected.index(topic) for topic in missing]
    items = [{"title": topic["title"], "methodology": topic.get("methodology", "")} for topic in missing]
    messages = prompts.build_messages(settings.system_prompt, prompts.build_completion_prompt(items, discipline))
    chunks: list[str] = []
    for piece in stream_chat_iter(client, messages, model_params or {}):
        chunks.append(piece)
        if include_raw and pending is not None:
            pending.append(piece)
            if sum(len(item) for item in pending) > 200:
                text = "".join(pending)
                pending.clear()
                yield _event("delta", text=text)
    if include_raw and pending:
        text = "".join(pending)
        pending.clear()
        yield _event("delta", text=text)

    parsed = extract_json("".join(chunks))
    if parsed is None:
        raise GenerationError("补简报的返回内容无法解析")
    batch = validate.normalize(parsed, discipline=discipline, requested=len(missing))
    filled = 0
    for position, topic in zip(indices, batch["topics"]):
        if not topic.get("brief_from_model"):
            continue
        collected[position]["brief"] = topic["brief"]
        collected[position]["brief_from_model"] = True
        filled += 1
    if filled:
        yield _event("stage", stage="brief", message=f"简报补齐完成（{filled}/{len(missing)}）", progress=0.97)


def generate_once(settings: Settings | None = None, **params) -> dict:
    """同步版：跑完整条流水线，返回最终结果。"""
    last: dict | None = None
    for event in generate_stream(settings, include_raw=False, **params):
        if event["type"] == "result":
            last = event["result"]
    if last is None:
        raise GenerationError("生成流程没有产出结果。")
    return last
