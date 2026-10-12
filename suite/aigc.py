"""降 AIGC 率客户端：把 ``E:\\AI\\aigc-reducer`` 那个**独立服务**接进本站。

为什么是"客户端"而不是"在本站再实现一遍"
------------------------------------------
降 AIGC 率不是一句提示词能解决的事。工作区里的 ``aigc-reducer`` 是一条已经调好的
闭环链路：本地体检（11 项风格指标 + 29 条规则）→ 分段并发改写 → 保真校验 →
复检（指数降了吗）→ 没降就把最差的几段连同**实测诊断**再改一轮 → 事实核查 → 收敛。

把这条链路抄进本站有两个坏处：一是它的依赖（fastapi / httpx / python-docx /
python-multipart）虽然与本项目完全相同，但**业务逻辑属于另一个项目**，抄过来就要
跟着它一起维护；二是它自带 Web 工作台，重写一份等于永远追不上它的更新。
所以这里只做**薄封装**：把它的 HTTP 接口包成本站能直接调的函数。

它不在本站进程里
----------------
这是本模块最容易踩的坑：``aigc-reducer`` 是一个**独立进程**，默认监听
``http://127.0.0.1:8770``。本站启动时不会顺带把它拉起来，所以最常见的失败就是
"服务没开"。因此本模块的每一个失败路径都必须说清楚**怎么启动它**
（见 :data:`START_HINT`），而不是把 ``ConnectError`` 原样丢给访客。

一个实测到的坑：**"服务没开"不一定表现为 ``ConnectError``。** 在本机的
沙箱/代理环境里，连一个没人监听的端口会返回 ``HTTP 502``（body 为空），
而 502 恰好也是"服务在跑但它调用模型失败"的状态码。两者的排查方向完全不同
（一个是启动服务，一个是查模型 .env），所以 502/503/504 一律**把两条路径都写进
错误信息**（见 :data:`UNREACHABLE_STATUS` 与 :func:`_unreachable_note`）。

安全护栏：主机白名单
--------------------
``SUITE_REDUCER_BASE`` 可以被环境变量覆盖，而本站会照着它发请求——这与
``suite/websearch.py`` 是同一类风险（把本站当成任意请求的跳板，SSRF）。
默认只放行 ``127.0.0.1`` / ``localhost``，自建部署时可设
``SUITE_ALLOW_ANY_REDUCER_HOST=1`` 解除。

**模型凭据不转发**（重要）
--------------------------
``aigc-reducer`` 的改写要调用大模型，但它的 Key **只从自己的 ``.env`` / 进程环境变量读**
（``app/config.py`` 的 ``load_settings()``），HTTP 请求体里只接受
``text / genre / intensity / target_score / max_rounds / auto_iterate /
concurrency / keep_terms / style_sample / consistency / fact_check`` —— **没有**
``base_url`` / ``api_key`` / ``model`` 这三个字段（见 ``app/server.py`` 的
``_run_kwargs``）。也就是说，**本站没有通道把访客的 Key 传过去**。

这不是可以糊过去的事，因为后果很具体：那个服务会用它自己 ``.env`` 里配的 Key 去改写，
于是**访客的改写量会记在站主的账上**。所以本模块：
不硬塞凭据（塞了也没用），而是在返回值和文档里如实写明这一点，
并把 :data:`CREDENTIAL_POLICY` 一并交给调用方展示。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# 地址与白名单
# ---------------------------------------------------------------------------

#: 降 AIGC 服务的默认地址。**这不是本站的地址**，是另一个进程的。
REDUCER_BASE = os.environ.get("SUITE_REDUCER_BASE", "http://127.0.0.1:8770")

#: 自建部署时可整体关掉白名单。
ALLOW_ANY_HOST = os.environ.get("SUITE_ALLOW_ANY_REDUCER_HOST", "").strip().lower() in {
    "1", "true", "yes", "on",
}

#: 默认只放行本机。允许 ``::1`` 是因为 IPv6 下的 localhost 会解析到它。
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: 启动方法。这条信息会出现在**每一个**连接失败的错误里——因为它就是用户唯一要做的事。
START_HINT = (
    "降 AIGC 服务没有响应。本站不自己跑改写模型，它依赖 E:\\AI\\aigc-reducer 这个独立服务。"
    "请先启动它：双击 E:\\AI\\aigc-reducer\\run.cmd（或在该目录下执行 run.cmd），"
    "它默认监听 http://127.0.0.1:8770，启动后访问 http://127.0.0.1:8770/api/health 应返回 "
    '{"ok":true}。如果它跑在别的地址上，请设置环境变量 SUITE_REDUCER_BASE 指向它。'
)

#: 诚实声明。**必须随返回值一起交给调用方**，不能只写在注释里——用户只看返回结果。
#: 原文见 ``E:\\AI\\aigc-reducer\\README.md``。
DISCLAIMER = (
    "AI 味指数是**本地的风格启发式代理**，**不是**知网 / 维普 / PaperPass "
    "那类商用 AIGC 检测器的输出，两者数值**不可互相换算**。"
    "本服务优化的是已被公开研究反复验证与检测器输出相关的风格信号"
    "（句长突发度、模板腔密度、标点多样性等），"
    "风格指标改善**不构成对任何具体检测器结果的承诺**，也无法承诺。"
)

#: 关于"谁付模型的钱"的如实说明。与 :data:`DISCLAIMER` 一样必须跟着返回值走。
CREDENTIAL_POLICY = (
    "aigc-reducer 的模型凭据由**它自己**配置（.env / 进程环境变量），"
    "本站**不转发**访客的 API Key：它的 HTTP 接口只接受改写参数，"
    "不接受 base_url / api_key / model。"
    "因此走这条链路时，模型调用的费用记在**那个服务所配置的 Key** 上，"
    "而不是访客自己的 Key。公网部署前请确认这一点是否符合你的预期。"
)

# ---------------------------------------------------------------------------
# 超时与上限
# ---------------------------------------------------------------------------
#: 探测类请求要短：服务没开时应当**立刻**报错，而不是让访客等半分钟。
TIMEOUT_PROBE = 8.0
#: 体检是纯本地计算，很快。
TIMEOUT_SCORE = 60.0
#: 改写要调很多次模型，一整篇学位论文可能要十几分钟，给足。
TIMEOUT_REDUCE = 1800.0
#: 导出 Word 只是本地排版。
TIMEOUT_EXPORT = 120.0
#: 流式读的超时：首字节之前可能等很久（首轮体检 + 建计划），之后是持续吐帧。
STREAM_TIMEOUT = httpx.Timeout(connect=8.0, read=600.0, write=60.0, pool=8.0)

#: 导出 Word 的字节上限。一份学位论文极少超过这个数，超了说明返回的不是文档。
MAX_EXPORT_BYTES = 40 * 1024 * 1024

#: 单次送进去的文本上限（字符）。**与上游的 40 万字上限保持一致**：
#: 在这里先拦住，比让上游返回一个 413 更容易讲清楚原因。
MAX_TEXT_CHARS = 400_000


#: 这三个状态码同时对应两种可能：服务根本没起来，或者服务起来了但它上游出错。
#:
#: 为什么要专门处理：**在本机的沙箱/代理环境里，连一个没有监听的端口不会抛
#: ``ConnectError``，而是返回一个 HTTP 502（body 为空）**——实测过。
#: 如果只按"502 = 改写时调模型失败"来解释，用户会去查模型的 .env，
#: 而真正的原因其实是"服务没开"，方向完全错了。
#: 分不清时就把两条排查路径都给出来，比给一条错的好。
UNREACHABLE_STATUS = {502, 503, 504}


def _unreachable_note(status: int) -> str:
    return (
        f"（HTTP {status}）降 AIGC 服务没有正常响应。两种可能，请依次排查："
        "① **服务没启动**：双击 E:\\AI\\aigc-reducer\\run.cmd，"
        '启动后访问 http://127.0.0.1:8770/api/health 应返回 {"ok":true}；'
        "② **服务在跑，但它自己调用模型失败**：检查该服务自己的 .env 里的 "
        "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。"
        "如果它跑在别的地址上，请设置环境变量 SUITE_REDUCER_BASE 指向它。"
    )


class AigcError(RuntimeError):
    """降 AIGC 链路失败。消息是中文、可直接展示给访客。"""


def _clean_text(value: Any) -> str:
    return str(value or "")


def _check_base(base: str) -> str:
    """校验服务地址。**构造客户端时就校验**，不要等到发请求才失败。

    与 ``suite/credentials.py:check_base_url`` / ``suite/websearch.py:_Base`` 同一套考虑：
    地址可以被环境变量覆盖，若不拦，本站就成了任意请求的跳板。
    """
    url = (base or "").strip()
    if not url:
        raise AigcError(f"降 AIGC 服务地址是空的。{START_HINT}")
    if not url.startswith(("http://", "https://")):
        raise AigcError(
            f"降 AIGC 服务地址必须以 http:// 或 https:// 开头，当前是「{url}」。"
            f"（默认地址是 {REDUCER_BASE}）"
        )
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise AigcError(f"无法从「{url}」里解析出主机名，请检查 SUITE_REDUCER_BASE 的写法。")
    if ALLOW_ANY_HOST or host in ALLOWED_HOSTS:
        return url.rstrip("/")
    raise AigcError(
        f"降 AIGC 服务地址不在白名单内（{host}）。"
        f"默认只允许 {'、'.join(sorted(ALLOWED_HOSTS))}。"
        f"确需连接远程服务时，请由站主设置 SUITE_ALLOW_ANY_REDUCER_HOST=1。"
    )


def startup_hint() -> str:
    """把"怎么把服务起起来"单独暴露出来，方便前端做成一键提示。"""
    return START_HINT


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class ReducerClient:
    """降 AIGC 服务的薄 HTTP 客户端。

    **每一次请求都现场读环境变量**（默认值取 :data:`REDUCER_BASE`），
    这样测试与自建部署改 ``SUITE_REDUCER_BASE`` 后无需重载模块。
    """

    def __init__(self, base: str | None = None, *, timeout: float = TIMEOUT_SCORE) -> None:
        raw = base if base is not None else os.environ.get("SUITE_REDUCER_BASE", REDUCER_BASE)
        self.base = _check_base(raw)
        self.timeout = timeout
        self._client: httpx.Client | None = None

    # -- 生命周期 ---------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            # 不跟随重定向：本地服务若被换成一个会 302 的地址，跟过去就把请求发出去了。
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "ReducerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------
    def _offline(self, exc: BaseException) -> str:
        """把连接类失败翻译成**可操作**的中文。

        这里刻意不把 ``ConnectError`` 原文放在最前面：访客看不懂，而真正的原因是
        "服务没开"这一件事。原始异常仍然附在末尾，供排查时核对。
        """
        return f"{START_HINT}（底层错误：{type(exc).__name__}: {exc}）"

    def _fail(self, exc: BaseException, *, doing: str) -> dict:
        """统一的失败返回。**不抛异常**，把可读错误放进 ``error``。"""
        if isinstance(exc, AigcError):
            return {"ok": False, "error": str(exc), "start_hint": START_HINT, "doing": doing}
        return {
            "ok": False,
            "error": self._offline(exc),
            "start_hint": START_HINT,
            "doing": doing,
        }

    def _url(self, path: str) -> str:
        return self.base + "/" + path.lstrip("/")

    def _json(self, resp: httpx.Response) -> Any:
        """解析响应体。上游返回 200 但不是 JSON 时（例如反代插了一页 HTML），要说清楚。"""
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            body = (resp.text or "")[:200].replace("\n", " ")
            raise AigcError(
                f"降 AIGC 服务返回的不是 JSON（HTTP {resp.status_code}）：{body}"
                f"（解析错误：{type(exc).__name__}）"
                f"——如果你在它前面挂了反向代理，请确认 /api/ 路径没有被改写。"
            ) from exc

    def _explain_status(self, resp: httpx.Response) -> str:
        """HTTP 错误码 -> 中文说明。上游的 400/502 都带 detail。"""
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or "")
        except Exception:  # noqa: BLE001 - 拿不到 detail 就退回纯状态码
            detail = (resp.text or "")[:200].replace("\n", " ")
        hints = {
            400: "请求被拒绝（通常是文本为空，或该服务未配置模型 Key）",
            404: "接口不存在——这个地址上的服务可能不是 aigc-reducer，或版本过旧",
            413: "文本太长，超过了服务端上限",
            422: "参数格式不对",
            500: "服务内部错误",
            502: "改写时调用模型失败（检查该服务自己的 .env 里的 LLM 配置）",
            504: "服务端超时",
        }
        if resp.status_code in UNREACHABLE_STATUS:
            # 见 UNREACHABLE_STATUS 的说明：这三种码既可能是"服务没起来"，
            # 也可能是"服务起来了但上游出错"，必须把两条路都给出来，
            # 否则用户会顺着错的那条查很久。
            message = f"HTTP {resp.status_code}：{hints.get(resp.status_code, '服务不可用')}"
            if detail:
                message += f"；服务返回：{detail}"
            return f"{message}｜{_unreachable_note(resp.status_code)}"
        hint = hints.get(resp.status_code, "调用失败")
        return f"HTTP {resp.status_code}：{hint}" + (f"；服务返回：{detail}" if detail else "")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        timeout: float | None = None,
        doing: str = "请求降 AIGC 服务",
    ) -> dict:
        """发一次请求，返回 ``{"ok": True, "data": ...}`` 或 ``{"ok": False, "error": ...}``。

        **永不抛异常**（``AigcError`` 也会被收进返回值）：上层是 Web 接口，
        一个 dict 比一个异常更容易原样转成 JSON 给访客看。
        """
        try:
            kwargs: dict[str, Any] = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            if payload is not None:
                kwargs["json"] = payload
            resp = self.client.request(method, self._url(path), **kwargs)
        except AigcError as exc:
            return self._fail(exc, doing=doing)
        except Exception as exc:  # noqa: BLE001 - 连接/超时类失败一律翻译
            return self._fail(exc, doing=doing)

        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": self._explain_status(resp),
                "start_hint": START_HINT,
                "doing": doing,
            }
        try:
            return {"ok": True, "data": self._json(resp)}
        except AigcError as exc:
            return self._fail(exc, doing=doing)

    # -- 高层接口 ---------------------------------------------------------
    def health(self) -> dict:
        """服务是否在线。顺带报告**它自己有没有配模型 Key**。

        为什么顺带查 ``/api/config``：那决定了访客的改写会不会记在站主的账上
        （见 :data:`CREDENTIAL_POLICY`）。站主需要一眼看到这件事。
        """
        probe = self.request("GET", "/api/health", timeout=TIMEOUT_PROBE, doing="探测服务是否在线")
        if not probe.get("ok"):
            return {
                "ok": False,
                "online": False,
                "base": self.base,
                "error": probe.get("error"),
                "start_hint": START_HINT,
            }
        data = probe.get("data") or {}
        out = {
            "ok": True,
            "online": True,
            "base": self.base,
            "version": str(data.get("version") or ""),
            "upstream_ok": bool(data.get("ok", True)),
            # 下面两项来自 /api/config，只报"有没有"，绝不回传 Key 本身。
            "has_own_llm_key": None,
            "own_llm_model": "",
        }
        cfg = self.request("GET", "/api/config", timeout=TIMEOUT_PROBE, doing="读取服务自身配置")
        if cfg.get("ok") and isinstance(cfg.get("data"), dict):
            llm = (cfg["data"].get("llm") or {})
            out["has_own_llm_key"] = bool(llm.get("has_api_key"))
            out["own_llm_model"] = str(llm.get("model") or "")
        if out["has_own_llm_key"]:
            out["credentials_note"] = CREDENTIAL_POLICY
        elif out["has_own_llm_key"] is False:
            out["credentials_note"] = (
                "降 AIGC 服务**没有**配置它自己的模型 Key，改写会失败。"
                "请在该服务的界面上或 E:\\AI\\aigc-reducer\\.env 里配置 LLM_API_KEY。"
                "注意：本站仍然无法转发访客的 Key（它的接口不接受该参数）。"
            )
        return out

    def metrics(self) -> dict:
        """11 项风格指标的说明与标定锚点。"""
        out = self.request("GET", "/api/metrics", timeout=TIMEOUT_PROBE, doing="读取指标说明")
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "start_hint": START_HINT}
        data = out.get("data") or {}
        data = dict(data) if isinstance(data, dict) else {"metrics": data}
        data["ok"] = True
        data["disclaimer"] = DISCLAIMER
        return data

    def rules(self) -> dict:
        """29 条规则清单（含中文特有的那几类）。"""
        out = self.request("GET", "/api/rules", timeout=TIMEOUT_PROBE, doing="读取规则清单")
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "start_hint": START_HINT}
        data = out.get("data") or {}
        data = dict(data) if isinstance(data, dict) else {"rules": data}
        data["ok"] = True
        data["disclaimer"] = DISCLAIMER
        return data

    def score(self, text: str, *, creds: Any = None) -> dict:
        """只体检、不改写：返回 AI 味指数与逐项指标。

        ``creds`` 参数**是故意收下但不使用的**：这个接口纯本地计算，不需要模型。
        收下它是为了让调用方用同一套签名调所有方法，不必去记哪个要凭据。
        """
        body = _clean_text(text)
        if not body.strip():
            return {"ok": False, "error": "文本为空，无法体检。", "start_hint": START_HINT}
        if len(body) > MAX_TEXT_CHARS:
            return {
                "ok": False,
                "error": f"文本过长（{len(body)} 字，上限 {MAX_TEXT_CHARS} 字），请分段体检。",
                "start_hint": START_HINT,
            }
        out = self.request("POST", "/api/score", payload={"text": body}, doing="体检文本")
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "start_hint": START_HINT}
        data = out.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": f"体检结果格式异常：{str(data)[:200]}"}
        data["ok"] = True
        # 声明**必须**跟着返回值走，不能只写在模块文档里。
        data["disclaimer"] = DISCLAIMER
        return data

    def guard(self, original: str, revised: str) -> dict:
        """保真校验：对照原文检查改写稿有没有改数字/丢引用/丢术语。

        为什么把它单独暴露出来：它检查的是**任意两次文本之间的差异**，
        所以用户拿别的工具改完的稿子也能先过一遍这道闸，而不只是检查本站产出。
        """
        if not _clean_text(original).strip() or not _clean_text(revised).strip():
            return {
                "ok": False,
                "error": "保真校验需要同时提供原文与改写稿。",
                "start_hint": START_HINT,
            }
        out = self.request(
            "POST",
            "/api/guard",
            payload={"original": _clean_text(original), "rewritten": _clean_text(revised)},
            doing="保真校验",
        )
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "start_hint": START_HINT}
        data = out.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": f"保真校验结果格式异常：{str(data)[:200]}"}
        # **不要覆盖上游的 ok**：上游的 ``ok`` 意思是"保真通过"（没有违规），
        # 而本模块的 ``ok`` 意思是"调用成功"。两者撞名，早先直接覆盖，
        # 结果是"查出了 2 处丢数字"被判成 ok=True——正好把结论说反了。
        # 这里把上游的判定挪到 ``passed``，``ok`` 只表示调用成功。
        passed = data.get("ok")
        if passed is None:
            passed = not (data.get("violations") or [])
        data["passed"] = bool(passed)
        data["verdict"] = "保真通过（未发现机械可查的改动）" if passed else "发现改动，需人工复核"
        data["ok"] = True
        data["limits"] = (
            "只做**机械可查**的比对（数字、引用角标、英文术语、长度比例），"
            "它抓不到「换了说法但意思变了」这类语义篡改。"
            "这一项是复核清单，不是判决。"
            "注意：``ok`` 表示**调用是否成功**，``passed`` 才是保真结论。"
        )
        return data

    def reduce_text(self, text: str, *, creds: Any = None, **opts: Any) -> dict:
        """同步改写（体检 → 改写 → 复检 → 收敛 → 事实核查）。

        ``creds`` 只用于**如实报告**，不会被转发——原因见模块文档与
        :data:`CREDENTIAL_POLICY`，结论写在返回值的 ``credentials_note`` 里。
        """
        body = _clean_text(text)
        if not body.strip():
            return {"ok": False, "error": "文本为空，无法改写。", "start_hint": START_HINT}
        if len(body) > MAX_TEXT_CHARS:
            return {
                "ok": False,
                "error": f"文本过长（{len(body)} 字，上限 {MAX_TEXT_CHARS} 字），请导入 Word 分批处理。",
                "start_hint": START_HINT,
            }
        payload = {"text": body}
        payload.update(self._clean_opts(opts))
        out = self.request(
            "POST", "/api/reduce", payload=payload, timeout=TIMEOUT_REDUCE, doing="改写文本"
        )
        return self._finalize_reduce(out, creds)

    def reduce_stream(self, text: str, *, creds: Any = None, **opts: Any) -> Iterator[dict]:
        """流式改写，逐帧产出 SSE 事件的 dict。

        事件类型（上游定义）：``baseline`` / ``plan`` / ``round`` / ``paragraph`` /
        ``round_end`` / ``fact_check`` / ``final``。这里**原样透传**，只补上
        ``disclaimer`` 与 ``credentials_note`` 两个字段，让前端在流式过程中也能显示声明。
        """
        body = _clean_text(text)
        if not body.strip():
            yield {"type": "error", "message": "文本为空，无法改写。", "start_hint": START_HINT}
            return
        if len(body) > MAX_TEXT_CHARS:
            yield {
                "type": "error",
                "message": f"文本过长（{len(body)} 字，上限 {MAX_TEXT_CHARS} 字）。",
                "start_hint": START_HINT,
            }
            return

        payload = {"text": body}
        payload.update(self._clean_opts(opts))
        note = self._credentials_note(creds)

        try:
            with self.client.stream(
                "POST", self._url("/api/reduce/stream"), json=payload, timeout=STREAM_TIMEOUT
            ) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", "replace")[:300]
                    if resp.status_code in UNREACHABLE_STATUS:
                        message = (
                            f"HTTP {resp.status_code}｜{_unreachable_note(resp.status_code)}"
                            + (f"；服务返回：{detail}" if detail.strip() else "")
                        )
                    else:
                        message = f"HTTP {resp.status_code}：{detail}"
                    yield {
                        "type": "error",
                        "message": message,
                        "start_hint": START_HINT,
                    }
                    return
                for frame in _iter_sse(resp):
                    frame.setdefault("disclaimer", DISCLAIMER)
                    frame.setdefault("credentials_note", note)
                    yield frame
        except Exception as exc:  # noqa: BLE001 - 流式失败也要变成可读的一帧
            yield {
                "type": "error",
                "message": self._offline(exc),
                "start_hint": START_HINT,
                "disclaimer": DISCLAIMER,
                "credentials_note": note,
            }

    def export_docx(self, text: str, *, title: str = "") -> bytes:
        """导出 Word，返回 ``.docx`` 的字节。

        上游的 ``/api/export/docx`` 只返回一个下载链接，所以要**两步走**：
        先 POST 拿 token，再 GET 把字节取回来。

        **本函数是唯一会抛异常的公开接口**：它的返回类型是 ``bytes``，
        没有地方放错误信息。抛出的仍是可读中文。
        """
        body = _clean_text(text)
        if not body.strip():
            raise AigcError("文本为空，无法导出 Word。")
        out = self.request(
            "POST",
            "/api/export/docx",
            payload={"text": body, "title": _clean_text(title)},
            timeout=TIMEOUT_EXPORT,
            doing="导出 Word",
        )
        if not out.get("ok"):
            raise AigcError(str(out.get("error") or "导出 Word 失败。"))

        data = out.get("data") or {}
        download = str((data or {}).get("download") or "")
        path = self._safe_download_path(download)
        try:
            resp = self.client.get(self._url(path), timeout=TIMEOUT_EXPORT)
        except Exception as exc:  # noqa: BLE001
            raise AigcError(self._offline(exc)) from exc
        if resp.status_code >= 400:
            raise AigcError(self._explain_status(resp))
        raw = resp.content or b""
        if not raw:
            raise AigcError("导出 Word 失败：服务返回了空文件。")
        if len(raw) > MAX_EXPORT_BYTES:
            raise AigcError(
                f"导出 Word 失败：返回了 {len(raw) / 1e6:.1f} MB，超过上限 "
                f"{MAX_EXPORT_BYTES // 1024 // 1024} MB。"
            )
        if not raw.startswith(b"PK"):
            raise AigcError("导出 Word 失败：返回的内容不是 .docx（缺少 zip 文件头）。")
        return raw

    # -- 内部工具 ---------------------------------------------------------
    @staticmethod
    def _safe_download_path(download: str) -> str:
        """校验下载路径。

        上游返回的是**相对路径**。如果它是绝对 URL，说明响应被改过或服务不是我们以为的
        那个——跟着它 GET 就等于照别人的话把请求发到第三方去，所以只接受
        ``/api/docx/download/<32 位十六进制>`` 这一种形状。
        """
        if not download:
            raise AigcError("导出 Word 失败：服务没有返回下载链接。")
        path = download.strip()
        if not path.startswith("/api/docx/download/"):
            raise AigcError(
                f"导出 Word 失败：下载链接形状不对（{path[:80]}）。"
                "只接受本站服务自己给出的 /api/docx/download/... 路径。"
            )
        token = path[len("/api/docx/download/"):].strip("/")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise AigcError(f"导出 Word 失败：下载标识不是合法的 token（{token[:40]}）。")
        return f"/api/docx/download/{token}"

    @staticmethod
    def _clean_opts(opts: dict) -> dict:
        """挑出上游认识的改写选项，并把空值丢掉。

        只做白名单透传：``_run_kwargs`` 读哪些字段，这里就放哪些，
        避免把调用方随手塞进来的东西（哪怕是凭据）转发出去。
        """
        allowed = {
            "genre", "intensity", "target_score", "max_rounds", "auto_iterate",
            "concurrency", "keep_terms", "style_sample", "consistency", "fact_check",
        }
        out: dict[str, Any] = {}
        for key, value in (opts or {}).items():
            if key in allowed and value is not None:
                out[key] = value
        return out

    @staticmethod
    def _credentials_note(creds: Any) -> str:
        """如实说明凭据去了哪里。**不允许说"已转发"**——那会让人以为费用是访客付的。"""
        configured = bool(getattr(creds, "configured", False)) if creds is not None else False
        note = (
            "本站**没有**把你的 API Key 转发给降 AIGC 服务——"
            "它的接口只接受改写参数（text/genre/intensity/...），不接受 base_url / api_key / model。"
            "本次改写的模型费用由**该服务自己配置的 Key** 承担。"
        )
        if configured:
            note += (
                "你填的 Key 本次未被使用；若希望用自己的 Key 改写，"
                "请改用本站的写作/评估链路，或直接在被调用的服务里配置你的 Key。"
            )
        return note

    def _finalize_reduce(self, out: dict, creds: Any) -> dict:
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "start_hint": START_HINT}
        data = out.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": f"改写结果格式异常：{str(data)[:200]}"}
        data["ok"] = True
        # 两个声明都跟着返回值走。
        data["disclaimer"] = DISCLAIMER
        data["credentials_note"] = self._credentials_note(creds)
        return data


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------
def _iter_sse(resp: httpx.Response) -> Iterator[dict]:
    """把 SSE 流解析成 dict 序列。

    **单帧坏掉不能中断整条流**——模型偶尔会吐半个 JSON，用户已经等了十分钟，
    因为一帧解析不了就把整个结果丢掉是不可接受的。所以坏帧跳过（但报出来）。
    """
    for line in resp.iter_lines():
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        elif line.startswith(":"):
            continue                      # SSE 心跳注释
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            yield {"type": "unparsed", "raw": line[:300]}
            continue
        if isinstance(obj, dict):
            yield obj
        else:
            yield {"type": "unparsed", "raw": str(obj)[:300]}


# ---------------------------------------------------------------------------
# 模块级薄封装
# ---------------------------------------------------------------------------
#: 说明：这些函数的签名是给本站 API 层用的。``creds`` 一律收下但不转发，
#: 原因见 :data:`CREDENTIAL_POLICY`。


def health() -> dict:
    """降 AIGC 服务是否在线。**永不抛异常**。"""
    try:
        with ReducerClient(timeout=TIMEOUT_PROBE) as client:
            return client.health()
    except AigcError as exc:
        return {"ok": False, "online": False, "error": str(exc), "start_hint": START_HINT}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "online": False,
            "error": f"探测降 AIGC 服务时出错：{type(exc).__name__}: {exc}",
            "start_hint": START_HINT,
        }


def metrics() -> dict:
    """指标说明。服务不可达时返回可读错误，不抛。"""
    return _call(lambda c: c.metrics())


def rules() -> dict:
    """规则清单。服务不可达时返回可读错误，不抛。"""
    return _call(lambda c: c.rules())


def score(text: str, *, creds: Any = None) -> dict:
    """体检文本，返回 AI 味指数与逐项指标。返回值里带 ``disclaimer``。"""
    return _call(lambda c: c.score(text, creds=creds))


def guard(original: str, revised: str) -> dict:
    """保真校验：对照原文检查改写稿有没有改数字/引用/术语。"""
    return _call(lambda c: c.guard(original, revised))


def reduce_text(text: str, *, creds: Any = None, **opts: Any) -> dict:
    """同步改写。返回值里带 ``disclaimer`` 与 ``credentials_note``。"""
    return _call(lambda c: c.reduce_text(text, creds=creds, **opts))


def reduce_stream(text: str, *, creds: Any = None, **opts: Any) -> Iterator[dict]:
    """流式改写，逐帧产出 dict。

    这里**先构造客户端再返回生成器**：如果地址不合法，调用方应当在调用点就看到错误，
    而不是等到开始迭代才炸（生成器函数体会推迟到第一次 ``next()`` 才执行）。

    另外**在生成器结束时关掉 HTTP 连接池**：调用方拿到的是一个迭代器，
    没法用 ``with`` 管它。不关的话每一次流式改写都会漏一个 ``httpx.Client``
    ——长时间运行的网站会慢慢攒出一堆没用的连接。用 ``finally`` 包一层是为了
    调用方中途放弃迭代（用户关了页面）时也能关掉。
    """
    try:
        client = ReducerClient(timeout=TIMEOUT_REDUCE)
    except AigcError as exc:
        return iter([{"type": "error", "message": str(exc), "start_hint": START_HINT}])

    def gen() -> Iterator[dict]:
        try:
            yield from client.reduce_stream(text, creds=creds, **opts)
        finally:
            client.close()

    return gen()


def export_docx(text: str, *, title: str = "") -> bytes:
    """导出 Word。**这是唯一会抛 :class:`AigcError` 的公开接口**（返回类型是 bytes）。"""
    with ReducerClient(timeout=TIMEOUT_EXPORT) as client:
        return client.export_docx(text, title=title)


def _call(fn) -> dict:
    """统一的模块级入口包装：构造客户端 + 兜底任何未预期异常。

    构造阶段就可能失败（地址不在白名单），所以 ``ReducerClient()`` 必须在 try 里面。
    """
    try:
        with ReducerClient() as client:
            return fn(client)
    except AigcError as exc:
        return {"ok": False, "error": str(exc), "start_hint": START_HINT}
    except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都变成可读中文
        return {
            "ok": False,
            "error": f"调用降 AIGC 服务时出现未预期的错误：{type(exc).__name__}: {exc}",
            "start_hint": START_HINT,
        }
