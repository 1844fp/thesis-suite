"""访客自带密钥（BYOK）的落点：把「本次请求的模型凭据」变成两套上游代码都能用的对象。

为什么必须做这一层
------------------
两套上游代码都从**服务器本地**取密钥，而且各自还有「借用邻居项目 .env」的回退：

* ``thesis-agents`` 的 ``load_settings()``：进程环境变量 > 本项目 .env > 邻居项目 .env
* ``thesis-topic-helper`` 的 ``load_settings()``：邻居项目 .env > 本项目 .env > 进程环境变量

在公网上这意味着两件绝对不能发生的事：
1. 访客不填密钥时，服务器自己的密钥会被拿去替他付费；
2. 只要有一处回退没堵住，Key 就会被写进日志/错误信息里泄露。

所以本站**一次都不调用上游的 ``load_settings()``**，而是在每次请求里：

* 从请求头读出访客凭据；
* 亲手构造上游需要的配置对象（``Settings`` / ``LLMConfig``）并注入；
* 访客没给凭据时，显式构造一个 ``api_key=""`` 的对象——上游据此判定「未配置」，
  自动走离线模式，而不是回退到服务器密钥。

并发安全
--------
凭据只存在于请求局部对象里，**不写 os.environ、不写任何全局变量**。
环境变量是进程级的，两个访客同时请求会互相覆盖——这正是不能用它的原因。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 允许的接口地址
# ---------------------------------------------------------------------------
#: 公网部署时，``base_url`` 完全由访客提供，服务器会照着它发请求。
#: 默认只放行已知服务商，避免本站被当成任意请求的跳板（SSRF）。
#: 自建 / 本地部署可设 ``SUITE_ALLOW_CUSTOM_BASE_URL=1`` 解除限制。
ALLOW_CUSTOM_BASE_URL = os.environ.get("SUITE_ALLOW_CUSTOM_BASE_URL", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ALLOWED_HOSTS = (
    "api.deepseek.com",
    "open.bigmodel.cn",
    "dashscope.aliyuncs.com",
    "api.moonshot.cn",
    "api.openai.com",
    "api.siliconflow.cn",
    "api.baichuan-ai.com",
    "api.minimax.chat",
    "ark.cn-beijing.volces.com",
    "api.hunyuan.cloud.tencent.com",
    "api.mistral.ai",
    "openrouter.ai",
    "generativelanguage.googleapis.com",
)

#: 请求头名。用 ``X-`` 前缀而不是 ``Authorization``，是因为本站自己不做鉴权，
#: 借用 Authorization 容易和反向代理的鉴权规则打架。
HEADER_KEY = "x-llm-key"
HEADER_BASE = "x-llm-base"
HEADER_MODEL = "x-llm-model"


class CredentialError(ValueError):
    """凭据不可用，消息可直接展示给访客。"""


#: 站点自己的模型凭据（环境变量配置，仅站主可见）。
#:
#: 为什么要有这一层：本站原本是纯 BYOK（访客自带 Key）。但公开部署时，
#: 让每个访客先去申请一个 API Key 会挡掉绝大多数人。所以加上"服务器出 Key"这条兜底：
#: **访客没填时用服务器的，填了就用他自己的**。
#:
#: 与上游"借用邻居项目 .env"的区别（这个区别很重要，别混）：
#: 上游那套是从**服务器磁盘上的其它项目**悄悄借密钥，访客不知道、也无法拒绝；
#: 这里是**站主显式配置**的一项部署设置，并且是否启用、用了谁的钱，
#: 都会通过 /api/topics/options 如实告诉访客。显式配置与静默借用是两件事。
SERVER_KEY_ENV = "SUITE_SERVER_LLM_KEY"
SERVER_BASE_ENV = "SUITE_SERVER_LLM_BASE"
SERVER_MODEL_ENV = "SUITE_SERVER_LLM_MODEL"


def server_credentials() -> Credentials:
    """读站点自己配置的模型凭据。没配就返回空凭据（功能退回 BYOK）。"""
    key = os.environ.get(SERVER_KEY_ENV, "").strip()
    if not key:
        return Credentials()
    return Credentials(
        api_key=key,
        base_url=os.environ.get(SERVER_BASE_ENV, "").strip(),
        model=os.environ.get(SERVER_MODEL_ENV, "").strip(),
    )


def server_key_enabled() -> bool:
    """站点是否配置了自己的 Key（前端据此决定还显不显示"填 Key"输入框）。"""
    return bool(os.environ.get(SERVER_KEY_ENV, "").strip())


@dataclass
class Credentials:
    """一次请求的模型凭据。``api_key`` 为空表示访客没配，应走离线/演示模式。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def masked(self) -> str:
        key = self.api_key.strip()
        if not key:
            return ""
        if len(key) <= 10:
            return key[:2] + "*" * max(len(key) - 2, 0)
        return f"{key[:5]}{'*' * 8}{key[-4:]}"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def check_base_url(base_url: str) -> str:
    """校验访客提供的接口地址。空串表示「用默认服务商」，是合法的。"""
    url = _clean(base_url)
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        raise CredentialError("接口地址必须以 http:// 或 https:// 开头。")
    if ALLOW_CUSTOM_BASE_URL:
        return url.rstrip("/")
    host = url.split("//", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    if host in ALLOWED_HOSTS:
        return url.rstrip("/")
    if host in {"127.0.0.1", "localhost"}:
        # 本机部署时允许连本地 Ollama。
        return url.rstrip("/")
    raise CredentialError(
        f"公网部署不接受任意接口地址（{host}）。"
        f"请使用内置服务商，或由站主设置 SUITE_ALLOW_CUSTOM_BASE_URL=1 后自带地址。"
    )


def from_headers(headers: Any) -> Credentials:
    """从请求头解析凭据，**访客没有时退回站点自己配置的**。

    优先级：访客填的 > 站点配置的 > 空（功能退回离线模式）。

    这里对每一档都**只取非空值**，不做"空串继续往下找"那种写法——
    本项目在 BYOK 防线里吃过那个亏：`os.environ.get(k) or env.get(k, "")`
    遇到空串会继续往下读，结果读到了别的项目的密钥。
    密钥回退必须是**显式的、只认非空**的。
    """
    get = headers.get
    visitor_key = _clean(get(HEADER_KEY))
    if visitor_key:
        return Credentials(
            api_key=visitor_key,
            base_url=check_base_url(get(HEADER_BASE)),
            model=_clean(get(HEADER_MODEL)),
        )

    # 访客没填：用站点自己的。
    server = server_credentials()
    if server.configured:
        # 访客可能只填了地址/模型而没填 Key——那也允许，地址更具体就用他的。
        base = check_base_url(get(HEADER_BASE)) or server.base_url
        model = _clean(get(HEADER_MODEL)) or server.model
        return Credentials(api_key=server.api_key, base_url=base, model=model)

    # 两边都没有：返回空凭据，由调用方决定走离线模式还是报错。
    return Credentials(
        api_key="",
        base_url=check_base_url(get(HEADER_BASE)),
        model=_clean(get(HEADER_MODEL)),
    )


# ---------------------------------------------------------------------------
# 构造上游配置对象
# ---------------------------------------------------------------------------
def provider_presets() -> dict:
    """可选的模型服务商清单（取自题目生成助手的内置预设，保持一处定义）。"""
    from core.config import PROVIDER_PRESETS

    return {
        key: {"label": item.get("label", key), "base_url": item["base_url"], "model": item["model"]}
        for key, item in PROVIDER_PRESETS.items()
    }


def _resolve(creds: Credentials, provider: str) -> tuple[str, str]:
    """决定本次使用的 base_url 与 model：访客显式给的最优先，否则用预设默认。"""
    presets = provider_presets()
    preset = presets.get(provider) or presets["deepseek"]
    base_url = creds.base_url or preset["base_url"]
    model = creds.model or preset["model"]
    return base_url, model


def topic_settings(creds: Credentials, provider: str = "deepseek", **overrides):
    """构造题目生成助手要的 ``core.config.Settings``。"""
    from core.config import Settings as TopicSettings

    base_url, model = _resolve(creds, provider or "deepseek")
    kwargs = {
        "base_url": base_url,
        "model": model,
        # 空 Key 是**故意**的：上游据此判定未配置，走离线知识库模式。
        "api_key": creds.api_key,
        "provider": provider or "deepseek",
    }
    kwargs.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return TopicSettings(**kwargs)


def thesis_llm_config(creds: Credentials, provider: str = "deepseek", **overrides):
    """构造写作 / 评估助手要的 ``app.config.LLMConfig``。"""
    from app.config import LLMConfig

    base_url, model = _resolve(creds, provider or "deepseek")
    kwargs = {
        "base_url": base_url,
        "model": model,
        "api_key": creds.api_key,
    }
    kwargs.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return LLMConfig(**kwargs)


def runtime_env(creds: Credentials, provider: str = "deepseek") -> dict:
    """给不得不用 env 的上游接口准备的临时环境变量（仅在请求作用域内使用）。

    ``thesis-agents`` 的部分 agent 会自己调 ``load_settings()``，那些路径没法注入
    对象。本函数只用于**极少数**必须走 env 的调用点，且调用方必须保证用完即还原。
    正常路径请用 :func:`thesis_llm_config`。
    """
    base_url, model = _resolve(creds, provider or "deepseek")
    return {"LLM_BASE_URL": base_url, "LLM_API_KEY": creds.api_key, "LLM_MODEL": model}
