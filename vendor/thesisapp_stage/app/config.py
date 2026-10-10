"""配置读取。

刻意不依赖 python-dotenv：`.env` 格式极简，自带解析器可以保留注释与顺序
（保存配置时逐行改写，不重排文件），这一点和 thesis-assistant 的做法一致。

配置来源优先级：环境变量 > .env 文件 > 默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

#: 数据目录可用环境变量整体重定向。测试必须用它。
#:
#: 为什么要有这个开关：测试会真的建立项目，而项目落在 ``data/projects`` 下。
#: 没有隔离时，跑几次测试就会在真实工作目录里堆出几十个"修复器测试"项目，
#: 界面上的项目列表随即变得不可用——**而且清理它们需要人工介入**。
#: 与其靠"记得事后清理"，不如让测试一开始就写到别处。
_TEST_DATA_DIR = os.environ.get("DSH_THESIS_DATA_DIR", "").strip()
DATA_DIR = Path(_TEST_DATA_DIR) if _TEST_DATA_DIR else (ROOT / "data")

RUNS_DIR = DATA_DIR / "runs"
PROJECTS_DIR = DATA_DIR / "projects"
LIBRARY_DIR = DATA_DIR / "library"
INBOX_DIR = DATA_DIR / "inbox"

#: 密钥回退来源。
#:
#: 本系统的生成模型与 thesis-assistant 是同一个（都走 OpenAI 兼容协议），
#: 而那个项目通常已经配好了 key。**复制一份密钥到本项目的 .env 会让两处
#: 逐渐不一致**（换 key 时忘了一处），所以这里选择"读它的、但不抄它"。
#: 本项目的 .env 一旦填了 LLM_API_KEY，就以自己的为准。
FALLBACK_ENV_PATHS = (
    Path(r"E:\AI\thesis-assistant\.env"),
    Path(r"E:\AI\aigc-reducer\.env"),
)

#: 会被回退查找的键。只回退模型接入信息，不回退端口、阈值这类本地设置——
#: 那些抄过来的值是错的。
FALLBACK_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

#: 本项目直接引用既有两个项目的依赖目录，不重复安装。
#: 顺序有意义：aigc-reducer\libs 里有 python-docx / lxml / pyyaml，
#: thesis-assistant\libs 里有 pymupdf / numpy / jieba。
LIB_DIRS = (
    Path(r"E:\AI\aigc-reducer\libs"),
    Path(r"E:\AI\thesis-assistant\libs"),
)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY = value`` .env file.

    Line comments (``#``) and blank lines are skipped. Values keep inner spaces;
    surrounding whitespace and one layer of matching quotes are stripped.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _get(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


def _get_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(_get(env, key, str(default)))
    except ValueError:
        return default


def _get_float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(_get(env, key, str(default)))
    except ValueError:
        return default


def _get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 300.0
    #: 审稿类任务要的是稳定复现，不是创造力。
    review_temperature: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def masked_key(self) -> str:
        k = self.api_key.strip()
        if not k:
            return ""
        if len(k) <= 10:
            return k[0] + "•" * (len(k) - 1)
        return f"{k[:5]}{'•' * 8}{k[-4:]}"


@dataclass
class PeerConfig:
    """既有服务的位置。跨进程调用，不复制它们的代码。"""

    #: thesis-assistant：论文 RAG 知识库
    rag_base: str = "http://127.0.0.1:8760"
    #: aigc-reducer：降 AIGC 率工作台
    reducer_base: str = "http://127.0.0.1:8770"
    #: 知识库的**收料目录**：把 PDF 放进这里再触发入库。
    #:
    #: 之所以要显式配置：入库动作发生在 thesis-assistant 进程里，它只认自己的
    #: ``inbox/``。本系统要把作者选的资料投进去，就必须知道那个目录在哪。
    #: 换一个知识库服务时改这一项即可，不需要改代码。
    rag_inbox: str = r"E:\AI\thesis-assistant\inbox"
    timeout: float = 600.0


@dataclass
class VerifyConfig:
    """验证层的开关与阈值。外部核验默认开启，但可在离线环境一键关掉。"""

    #: 外部可核验：按 DOI / 标题查 Crossref，抓 AI 编造的参考文献
    external_enabled: bool = True
    crossref_base: str = "https://api.crossref.org"
    #: 未显式声明 DOI 时，是否允许用标题模糊检索（慢，且可能误判）
    crossref_title_search: bool = False
    #: 标题模糊匹配通过阈值（0~1）
    title_match_threshold: float = 0.82
    #: 单次验证最多外部查询多少条参考文献
    max_external_lookups: int = 60
    #: 请求间隔秒数，尊重 Crossref 的礼貌池
    external_delay: float = 0.2
    #: 礼貌池要求带真实联系邮箱，否则可能被限流
    contact_email: str = ""


@dataclass
class PipelineConfig:
    max_rounds: int = 3
    """自我更正最多迭代几轮。到顶仍不收敛就如实上报，不假装成功。"""


@dataclass
class Settings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    peers: PeerConfig = field(default_factory=PeerConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    host: str = "127.0.0.1"
    port: int = 8780
    env_path: Path = ENV_PATH
    #: 生成模型的密钥是从哪儿来的（本项目 .env / 某个邻居项目的 .env / 某个模型档案）。
    #: 界面需要显示它，否则"我明明没配 key，怎么就能用了"会让人困惑。
    llm_key_source: str = ""
    #: 多模型档案（``models.json``）。为空表示只有 .env 的单模型配置。
    models: Any = None

    def public(self) -> dict:
        """给界面看的配置视图。密钥只出脱敏串。"""
        return {
            "llm": {
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "temperature": self.llm.temperature,
                "max_tokens": self.llm.max_tokens,
                "has_api_key": bool(self.llm.api_key.strip()),
                "api_key_masked": self.llm.masked_key(),
                "configured": self.llm.configured,
                "key_source": self.llm_key_source or ("本次配置" if self.llm.configured else ""),
            },
            "models": self.models.public() if self.models is not None else None,
            "peers": {
                "rag_base": self.peers.rag_base,
                "reducer_base": self.peers.reducer_base,
                "rag_inbox": self.peers.rag_inbox,
            },
            "verify": {
                "external_enabled": self.verify.external_enabled,
                "crossref_title_search": self.verify.crossref_title_search,
                "max_external_lookups": self.verify.max_external_lookups,
                "contact_email": self.verify.contact_email,
                "title_match_threshold": self.verify.title_match_threshold,
            },
            "pipeline": {"max_rounds": self.pipeline.max_rounds},
            "server": {"host": self.host, "port": self.port},
            "lib_dirs": [str(p) for p in LIB_DIRS],
        }


def load_settings(env_path: Path | None = None) -> Settings:
    path = env_path or ENV_PATH
    env = parse_env_file(path)

    # 缺 key 时按顺序找邻居项目借用，并记下来源以便在界面上说明。
    key_source = "本次配置" if env.get("LLM_API_KEY", "").strip() else ""
    for fb in FALLBACK_ENV_PATHS:
        if key_source:
            break
        if not fb.exists() or fb.resolve() == path.resolve():
            continue
        other = parse_env_file(fb)
        for k in FALLBACK_KEYS:
            v = (other.get(k) or "").strip()
            if not v:
                continue
            if k == "LLM_API_KEY" and not key_source:
                env.setdefault(k, v)
                key_source = str(fb)
            else:
                env.setdefault(k, v)

    def _get_env(key: str, default: str = "") -> str:
        """Like ``_get`` but does not let an empty .env value mask the fallback."""
        return os.environ.get(key) or env.get(key) or default

    llm = LLMConfig(
        base_url=_get_env("LLM_BASE_URL", LLMConfig.base_url),
        api_key=os.environ.get("LLM_API_KEY") or env.get("LLM_API_KEY", ""),
        model=_get_env("LLM_MODEL", LLMConfig.model),
        temperature=_get_float(env, "LLM_TEMPERATURE", LLMConfig.temperature),
        max_tokens=_get_int(env, "LLM_MAX_TOKENS", LLMConfig.max_tokens),
        timeout=_get_float(env, "LLM_TIMEOUT", LLMConfig.timeout),
        review_temperature=_get_float(env, "REVIEW_TEMPERATURE", 0.0),
    )
    peers = PeerConfig(
        rag_base=_get(env, "RAG_BASE_URL", PeerConfig.rag_base),
        reducer_base=_get(env, "REDUCER_BASE_URL", PeerConfig.reducer_base),
        rag_inbox=_get(env, "RAG_INBOX_DIR", PeerConfig.rag_inbox),
        timeout=_get_float(env, "PEER_TIMEOUT", PeerConfig.timeout),
    )
    verify = VerifyConfig(
        external_enabled=_get_bool(env, "VERIFY_EXTERNAL", True),
        crossref_base=_get(env, "CROSSREF_BASE", VerifyConfig.crossref_base),
        crossref_title_search=_get_bool(env, "CROSSREF_TITLE_SEARCH", False),
        title_match_threshold=_get_float(env, "TITLE_MATCH_THRESHOLD", 0.82),
        max_external_lookups=_get_int(env, "MAX_EXTERNAL_LOOKUPS", 60),
        external_delay=_get_float(env, "EXTERNAL_DELAY", 0.2),
        contact_email=_get(env, "CONTACT_EMAIL", ""),
    )
    pipeline = PipelineConfig(max_rounds=_get_int(env, "MAX_ROUNDS", 3))

    settings = Settings(
        llm=llm,
        peers=peers,
        verify=verify,
        pipeline=pipeline,
        host=_get(env, "HOST", "127.0.0.1"),
        port=_get_int(env, "PORT", 8780),
        env_path=path,
    )
    settings.llm_key_source = key_source

    # ---- 多模型档案（models.json）------------------------------------
    #
    # 有档案就**以档案为准**：`LLM_*` 保留下来当各档案缺失字段的兜底
    # （这样"只改了 base_url、key 沿用 .env"这种常见情形不用重复填 key）。
    # 没有档案时一切照旧——老配置不需要改一个字。
    try:
        from .models import ModelRegistry

        reg = ModelRegistry.load()
        settings.models = reg
        active = reg.active_profile()
        if active is not None:
            settings.llm = active.to_llm_config(settings.llm)
            # 密钥来源要如实说：档案里没填 key 时，实际用的是 .env 里那把——
            # 这时写"来自模型档案"会让人以为密钥存在 models.json 里，
            # 下次清理文件时就会以为"清掉档案就等于删掉密钥了"。
            if active.api_key.strip():
                settings.llm_key_source = f"模型档案「{active.label or active.id}」"
            else:
                settings.llm_key_source = (
                    f"{key_source or '.env'}（档案「{active.label or active.id}」未填密钥，沿用此处）"
                )
    except Exception as exc:  # noqa: BLE001 - 档案坏了不该让整个系统起不来
        from .models import ModelRegistry

        reg = ModelRegistry()
        reg.problems.append(f"加载模型档案失败：{type(exc).__name__}: {exc}")
        settings.models = reg
    return settings


def ensure_dirs() -> None:
    for d in (DATA_DIR, RUNS_DIR, PROJECTS_DIR, LIBRARY_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    """Rewrite only the given keys, preserving every comment and line order.

    An empty-string value clears the key; ``None`` is not accepted here because
    "keep the old secret" is a caller-level decision, not a file-format one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        example = ROOT / ".env.example"
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pending = dict(updates)
    out: list[str] = []

    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key} = {pending.pop(key)}")
                continue
        out.append(raw)

    for key, value in pending.items():
        out.append(f"{key} = {value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
