"""配置解析：项目 .env → 环境变量 → 复用工作区既有项目的 Key。

优先级（低的在前，高的覆盖）：内置默认值 < 复用他人的配置 < .env 文件
< 已存在的环境变量 < 命令行参数。

"复用他人的配置" 是为了省事：工作区里 thesis-assistant 与 thesis-agents
已经配好了可用的 DeepSeek Key，本工具默认借用，用户不必再填一遍。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
WEB_DIR = PROJECT_ROOT / "web"
EXPORT_DIR = PROJECT_ROOT / "exports"
CACHE_DIR = PROJECT_ROOT / ".cache"

# 工作区里可以考虑借用配置的邻居项目（按顺序尝试）
SIBLING_ENV_FILES = (
    PROJECT_ROOT.parent / "thesis-assistant" / ".env",
    PROJECT_ROOT.parent / "thesis-agents" / ".env",
)
SIBLING_MODELS_JSON = PROJECT_ROOT.parent / "thesis-agents" / "models.json"

# ---------------------------------------------------------------------------
# 常见 OpenAI 兼容服务商预设
# ---------------------------------------------------------------------------
PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "note": "中文表现好、便宜，支持 JSON 输出，推荐。",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "note": "国内直连快，长文稳定。",
    },
    "dashscope": {
        "label": "通义千问（阿里云）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "note": "兼容模式，qwen-max 质量更高、更贵。",
    },
    "moonshot": {
        "label": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "note": "长上下文，适合一次性产出大量题目。",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "note": "需要可用的网络出口。",
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:14b",
        "note": "完全离线，不产生费用，质量取决于本地模型。",
    },
}

DEFAULT_SYSTEM_TEMPLATE = (
    "你是中文学术界的资深选题导师，长期指导本科毕业论文、硕士学位论文与"
    "期刊论文的选题与开题。你的强项是把一个宽泛的学科方向收敛成若干个"
    "可落地、有创新点、有明确数据来源和方法的选题，并说清每个选题的"
    "文献脉络与研究缺口。"
)


@dataclass
class Settings:
    base_url: str = PROVIDER_PRESETS["deepseek"]["base_url"]
    api_key: str = ""
    model: str = PROVIDER_PRESETS["deepseek"]["model"]
    temperature: float = 1.0
    max_tokens: int = 8000
    timeout: float = 180.0
    json_mode: bool = True
    provider: str = "deepseek"
    system_prompt: str = DEFAULT_SYSTEM_TEMPLATE
    sources: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """是否具备调用大模型的条件。"""
        if self.provider == "ollama":
            return bool(self.base_url)
        return bool(self.api_key)

    @property
    def key_masked(self) -> str:
        return mask_key(self.api_key)

    def describe(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "provider": self.provider,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "json_mode": self.json_mode,
            "has_key": bool(self.api_key),
            "ready": self.ready,
            "key_masked": self.key_masked,
            "sources": list(self.sources),
        }


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]}"


def parse_env_file(path: Path) -> dict:
    """极简 .env 解析：支持 KEY=VALUE、# 注释、行内注释与引号。"""
    values: dict = {}
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:  # 去掉行尾注释（仅当 # 前面有空白）
            for idx, ch in enumerate(value):
                if ch == "#" and idx > 0 and value[idx - 1] in " \t":
                    value = value[:idx].strip()
                    break
        values[key] = value
    return values


def _from_siblings() -> tuple[dict, list]:
    """从邻居项目借用 LLM 配置。返回 (键值对, 来源说明)。"""
    found: dict = {}
    sources: list = []

    for path in SIBLING_ENV_FILES:
        if not path.exists():
            continue
        env = parse_env_file(path)
        borrowed = {}
        if env.get("LLM_BASE_URL") and "base_url" not in found:
            borrowed["base_url"] = env["LLM_BASE_URL"]
        if env.get("LLM_API_KEY") and "api_key" not in found:
            borrowed["api_key"] = env["LLM_API_KEY"]
        if env.get("LLM_MODEL") and "model" not in found:
            borrowed["model"] = env["LLM_MODEL"]
        found.update(borrowed)
        if borrowed:
            sources.append(f"{path}（借用：{', '.join(sorted(borrowed))}）")

    if "api_key" not in found and SIBLING_MODELS_JSON.exists():
        try:
            data = json.loads(SIBLING_MODELS_JSON.read_text(encoding="utf-8"))
            for profile in data.get("profiles", []):
                if profile.get("api_key"):
                    found["api_key"] = profile["api_key"]
                    found.setdefault("base_url", profile.get("base_url") or "")
                    found.setdefault("model", profile.get("model") or "")
                    sources.append(f"{SIBLING_MODELS_JSON}（借用：api_key）")
                    break
        except (OSError, ValueError):
            pass
    return found, sources


def _coerce(env: dict, key: str, cast, default):
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def load_settings(overrides: dict | None = None) -> Settings:
    """装配最终配置。overrides 里值为 None/"" 的项会被忽略。"""
    base, sources = _from_siblings()

    if ENV_PATH.exists():
        env = parse_env_file(ENV_PATH)
        used = [key for key, value in env.items() if value and key.startswith("LLM_")]
        for key, value in env.items():
            if not value:
                continue
            # LLM_TEMPERATURE -> temperature，否则下面的 _coerce 永远取不到值
            name = key[4:].lower() if key.startswith("LLM_") else key.lower()
            base[name] = value
        if used:
            sources.append(f"{ENV_PATH}（{'、'.join(sorted(used))}）")

    # 环境变量优先级高于 .env
    for env_key, field_name in (
        ("LLM_BASE_URL", "base_url"),
        ("LLM_API_KEY", "api_key"),
        ("LLM_MODEL", "model"),
    ):
        value = os.environ.get(env_key)
        if value:
            base[field_name] = value
            sources.append(f"环境变量 {env_key}")

    settings = Settings(
        base_url=base.get("base_url") or Settings.base_url,
        api_key=base.get("api_key") or "",
        model=base.get("model") or Settings.model,
        temperature=_coerce(base, "temperature", float, 1.0),
        max_tokens=_coerce(base, "max_tokens", int, 8000),
        timeout=_coerce(base, "timeout", float, 180.0),
        json_mode=str(base.get("json_mode", "1")).lower() not in ("0", "false", "no"),
        provider=base.get("provider") or "",
        system_prompt=base.get("system_prompt") or DEFAULT_SYSTEM_TEMPLATE,
        sources=sources,
    )
    if settings.provider and settings.provider in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[settings.provider]
        if not base.get("base_url"):
            settings.base_url = preset["base_url"]
        if not base.get("model"):
            settings.model = preset["model"]
    if overrides:
        clean = {k: v for k, v in overrides.items() if v not in (None, "")}
        settings = replace(settings, **clean)
    return settings


def ensure_dirs() -> None:
    for path in (EXPORT_DIR, CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)


ENV_TEMPLATE = """\
# ---------------------------------------------------------------------------
# 论文选题助手 · 配置
#
# 直接改本文件即可，改完重启服务生效（或在网页右上角「设置」里改）。
# 本工具默认会借用 ..\\thesis-assistant\\.env 里已配好的 Key，
# 所以通常你什么都不用填就能直接用。
# ---------------------------------------------------------------------------

# === 生成模型（任意 OpenAI 兼容接口）=====================================
# DeepSeek   https://api.deepseek.com/v1                        deepseek-chat
# 智谱       https://open.bigmodel.cn/api/paas/v4               glm-4-plus
# 通义       https://dashscope.aliyuncs.com/compatible-mode/v1   qwen-plus
# Kimi       https://api.moonshot.cn/v1                          moonshot-v1-32k
# 本地 Ollama http://127.0.0.1:11434/v1                          qwen2.5:14b
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL=deepseek-chat

# 选题属于创造性任务，温度高一点题目更多样（0.8 ~ 1.3 比较合适）
LLM_TEMPERATURE=1.0

# 一次生成 8 个带完整简报的题目，输出较长，建议不低于 6000
LLM_MAX_TOKENS=8000
LLM_TIMEOUT=180

# 要求模型返回严格 JSON（个别服务商不支持时改成 0）
LLM_JSON_MODE=1
"""
