"""多模型接入：配置档案、厂商预设、按用途分配、连通性测试。

## 为什么需要这一层

一套论文生成要发起 **40~60 次模型调用**，而这些调用的性质完全不同：

* **起草**是最重要的——它决定成稿的质量，值得用最强的模型；
* **规划、结论汇总、改写**次之；
* **模型检查器与自动修复**是判断句和机械改写，用便宜模型完全够用。

原先全局只有一个模型，作者面对的是一个两难：用好模型，一次生成几十次调用
花掉不少钱；用便宜模型，正文质量跟着掉。**按用途分开配就解决了这个两难**——
起草用强模型，核验与修复用便宜模型，成本能降下来而正文质量不受影响。

另一件必须解决的事：**换厂商不该靠改代码或手抠 .env**。国内可用的服务商
（DeepSeek、智谱、通义、Kimi、豆包、本地 Ollama……）都兼容 OpenAI 协议，
差别只在 base_url 与模型名。所以这里把常见厂商做成**预设**，作者选一个、
填个 key 就能用。

## 配置放在哪，以及为什么不是 .env

档案存在项目根目录的 ``models.json``，**不动 .env**。理由：

* ``.env`` 是扁平 ``KEY=VALUE``，而"多份档案 + 一份用途分配表"是嵌套结构，
  硬塞进去要么改成 JSON 值（可读性没了），要么摊成 ``MODEL_1_BASE_URL`` 这种
  编造出来的键名（改一个档案要改六行，还容易改漏）；
* ``write_env_updates`` 的价值在于"保留注释与行序、只改指定键"——
  让它去写 JSON 会破坏这个性质。

两个文件的信任级别相同（同一个目录、同样只在本机），所以这不增加风险。
``.env`` 里的 ``LLM_*`` 仍然有效，并且**在没有 models.json 时就是默认配置**——
老配置不需要动。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from .config import ROOT, LLMConfig

__all__ = [
    "ModelProfile",
    "Preset",
    "PRESETS",
    "ROLES",
    "role_of",
    "ModelRegistry",
    "ModelRouter",
    "MODELS_PATH",
    "preset_list",
]

#: 档案文件位置。与 .env 同级，方便备份与手动查看。
MODELS_PATH = ROOT / "models.json"

#: 用途。顺序即界面上的显示顺序。
#:
#: 每一项都是"一次生成里性质不同的一类调用"。分开配的意义见模块 docstring。
ROLES: tuple[tuple[str, str], ...] = (
    ("draft", "起草正文（最关键、最长）"),
    ("plan", "写作规划与大纲"),
    ("conclude", "结论汇总"),
    ("rewrite", "按你的要求改写"),
    ("verify", "模型检查器（审稿、核验）"),
    ("fix", "自动修复"),
    ("polish", "润色"),
    ("other", "其它（外部核验、抽取等）"),
)

#: purpose 前缀 → 用途。``ask_model`` 的 purpose 形如 ``writer:draft:绪论#1``。
_ROLE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("writer:draft", "draft"),
    ("writer:outline", "plan"),
    ("writer:plan", "plan"),
    ("writer:conclude", "conclude"),
    ("writer:rewrite", "rewrite"),
    ("writer:polish", "polish"),
    ("fixer", "fix"),
    ("fix", "fix"),
    ("verify", "verify"),
    ("check", "verify"),
)


def role_of(purpose: str) -> str:
    """把一次调用的 purpose 归到一个用途上。

    归类的粒度刻意粗：作者能理解的分类是"起草/核验/修复"这几档，
    而不是"writer:draft:第三章#2"这种内部标签。分得太细，配置界面上就没人配得动。
    """
    p = (purpose or "").strip().lower()
    for prefix, role in _ROLE_PREFIXES:
        if p.startswith(prefix):
            return role
    return "other"


# ---------------------------------------------------------------------------
# 厂商预设
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """一个厂商的接入信息。

    ``model`` 是**建议值**，不是承诺：各家的模型名会变（上新、下线、改名），
    而工具没法跟着变。所以界面上这个字段始终可改，文档里也写明"以厂商文档为准"。
    """

    id: str
    label: str
    base_url: str
    model: str
    note: str = ""
    needs_key: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


PRESETS: tuple[Preset, ...] = (
    Preset("deepseek", "DeepSeek 深度求索", "https://api.deepseek.com/v1", "deepseek-chat",
           "性价比高，中文写作表现稳定。deepseek-reasoner 是推理模型，慢但更会分析。"),
    Preset("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus",
           "国产老牌，glm-4-flash 很便宜，适合拿来做核验与修复。"),
    Preset("dashscope", "阿里通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
           "走 DashScope 的 OpenAI 兼容模式。qwen-turbo 便宜、qwen-max 强。"),
    Preset("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-32k",
           "长上下文友好，适合一次读进大量证据。"),
    Preset("ark", "火山方舟（豆包）", "https://ark.cn-beijing.volces.com/api/v3", "",
           "模型名要填你在方舟上创建的**接入点 ID**（ep-…），不是模型代号。"),
    Preset("baichuan", "百川智能", "https://api.baichuan-ai.com/v1", "Baichuan4",
           ""),
    Preset("minimax", "MiniMax", "https://api.minimax.chat/v1", "abab6.5s-chat",
           ""),
    Preset("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini",
           "需要能直连 api.openai.com。"),
    Preset("ollama", "本地 Ollama", "http://127.0.0.1:11434/v1", "qwen2.5:14b",
           "完全离线、不要 key。填任意占位字符串即可（如 ollama）。"
           "本地模型适合做核验与修复，起草质量取决于模型本身。",
           needs_key=False),
    Preset("vllm", "本地 vLLM / LM Studio / 其它 OpenAI 兼容服务",
           "http://127.0.0.1:8000/v1", "", "自建推理服务的地址；不需要 key 时随便填一个占位值。",
           needs_key=False),
    Preset("custom", "自定义", "", "", "手填 base_url 与模型名。任何 OpenAI 兼容的服务都能接。",
           needs_key=False),
)


def preset_list() -> list[dict]:
    return [p.as_dict() for p in PRESETS]


def get_preset(preset_id: str) -> Preset | None:
    for p in PRESETS:
        if p.id == preset_id:
            return p
    return None


# ---------------------------------------------------------------------------
# 模型档案
# ---------------------------------------------------------------------------


def _f(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class ModelProfile:
    """一份模型接入配置。"""

    id: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 300.0
    #: 从哪个预设建的（只作记录，便于界面显示来源）
    preset: str = ""
    note: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip()) and bool(self.base_url.strip()) and bool(self.model.strip())

    def masked_key(self) -> str:
        k = self.api_key.strip()
        if not k:
            return ""
        if len(k) <= 10:
            return k[0] + "•" * (len(k) - 1)
        return f"{k[:5]}{'•' * 8}{k[-4:]}"

    def to_llm_config(self, base: LLMConfig | None = None) -> LLMConfig:
        """转成 ``LLMClient`` 需要的配置。未填的字段沿用 ``base``（即 .env 的值）。"""
        b = base or LLMConfig()
        return replace(
            b,
            base_url=self.base_url.strip() or b.base_url,
            api_key=self.api_key.strip() or b.api_key,
            model=self.model.strip() or b.model,
            temperature=_f(self.temperature, b.temperature),
            max_tokens=_i(self.max_tokens, b.max_tokens),
            timeout=_f(self.timeout, b.timeout),
        )

    def to_dict(self) -> dict:
        """**落盘用**：完整数据，含明文密钥。

        刻意与 ``public()`` 分成两个函数。原先 ``save()`` 复用了 ``public(mask=False)``，
        而 ``public()`` 的字段叫 ``api_key_masked``——存进去是这个名字，``from_dict``
        读的却是 ``api_key``。结果：**密钥写进文件又读不回来**，界面上显示"已填全"、
        重启后变成"未填全"，而文件里那个键名看着还挺像回事，很难发现。

        "给界面看的"和"存盘用的"是两件事：前者要脱敏、字段为展示服务；
        后者要完整、字段为往返一致服务。共用一个函数就会出这种事。
        """
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "preset": self.preset,
            "note": self.note,
        }

    def public(self, *, mask: bool = True) -> dict:
        """**给界面看**：密钥只出脱敏串。"""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "preset": self.preset,
            "note": self.note,
            "has_api_key": bool(self.api_key.strip()),
            "api_key_masked": self.masked_key(),
            "configured": self.configured,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ModelProfile":
        pid = str(raw.get("id") or "").strip()
        return cls(
            id=pid,
            label=str(raw.get("label") or "").strip(),
            base_url=str(raw.get("base_url") or "").strip(),
            api_key=str(raw.get("api_key") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            temperature=_f(raw.get("temperature"), 0.2),
            max_tokens=_i(raw.get("max_tokens"), 4096),
            timeout=_f(raw.get("timeout"), 300.0),
            preset=str(raw.get("preset") or "").strip(),
            note=str(raw.get("note") or "").strip(),
        )

    @classmethod
    def from_preset(cls, preset_id: str, *, pid: str = "", api_key: str = "", model: str = "") -> "ModelProfile":
        p = get_preset(preset_id)
        if p is None:
            raise ValueError(f"未知的厂商预设：{preset_id}")
        return cls(
            id=(pid or p.id).strip(),
            label=p.label,
            base_url=p.base_url,
            api_key=api_key.strip(),
            model=(model or p.model).strip(),
            preset=p.id,
            note=p.note,
        )


@dataclass
class ModelRegistry:
    """全部档案 + 当前激活 + 用途分配。"""

    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    active: str = ""
    #: 用途 → 档案 id。缺省表示"用当前激活的档案"。
    roles: dict[str, str] = field(default_factory=dict)
    path: Path = MODELS_PATH
    #: 加载时的问题（文件坏了等），供界面如实显示而不是静默用空配置。
    problems: list[str] = field(default_factory=list)

    # -- 读 ---------------------------------------------------------------
    @property
    def configured_profiles(self) -> list[ModelProfile]:
        return [p for p in self.profiles.values() if p.configured]

    def active_profile(self) -> ModelProfile | None:
        if self.active and self.active in self.profiles:
            return self.profiles[self.active]
        # 激活项失效时退回第一个填全了的档案——比"什么都没配"有用。
        done = self.configured_profiles
        return done[0] if done else None

    def resolve(self, role: str) -> ModelProfile | None:
        """用途 → 档案。没单独配就落到当前激活的档案。"""
        pid = self.roles.get(role or "", "")
        if pid and pid in self.profiles:
            return self.profiles[pid]
        return self.active_profile()

    def missing_roles(self) -> list[str]:
        """哪些用途被指向了不存在或没填全的档案。界面要据此报警。"""
        out: list[str] = []
        for role, _desc in ROLES:
            pid = self.roles.get(role, "")
            if not pid:
                continue
            prof = self.profiles.get(pid)
            if prof is None:
                out.append(f"{role} → 「{pid}」这个档案不存在")
            elif not prof.configured:
                out.append(f"{role} → 「{prof.label or pid}」还没填全（base_url / 模型名 / key）")
        return out

    # -- 写 ---------------------------------------------------------------
    def upsert(self, profile: ModelProfile) -> None:
        if not profile.id:
            raise ValueError("档案 id 不能为空")
        self.profiles[profile.id] = profile
        if not self.active:
            self.active = profile.id

    def remove(self, pid: str) -> bool:
        if pid not in self.profiles:
            return False
        del self.profiles[pid]
        if self.active == pid:
            self.active = next(iter(self.profiles), "")
        # 用途指向被删档案时清掉，避免留下一个悬空引用
        self.roles = {r: p for r, p in self.roles.items() if p != pid}
        return True

    def set_role(self, role: str, pid: str) -> None:
        if pid:
            self.roles[role] = pid
        else:
            self.roles.pop(role, None)

    # -- 序列化 -----------------------------------------------------------
    def as_dict(self, *, mask: bool = True) -> dict:
        return {
            "version": 1,
            "active": self.active,
            "roles": dict(self.roles),
            "profiles": [
                (p.public() if mask else p.to_dict()) for p in self.profiles.values()
            ],
        }

    def public(self) -> dict:
        """给界面看的视图：密钥只出脱敏串。"""
        active = self.active_profile()
        return {
            "active": self.active,
            "active_label": (active.label or active.id) if active else "",
            "profiles": [p.public() for p in self.profiles.values()],
            "roles": {r: self.roles.get(r, "") for r, _ in ROLES},
            "role_defs": [{"id": r, "label": d} for r, d in ROLES],
            "role_warnings": self.missing_roles(),
            "problems": list(self.problems),
            "path": str(self.path),
            "configured": bool(self.configured_profiles),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 明文存 key：它与 .env 在同一目录、同一信任级别。文件权限交由操作系统。
        payload = json.dumps(self.as_dict(mask=False), ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: Path | None = None) -> "ModelRegistry":
        p = path or MODELS_PATH
        reg = cls(path=p)
        if not p.exists():
            return reg
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 文件坏了要如实说，不能静默当空配置
            reg.problems.append(
                f"读取 {p.name} 失败：{type(exc).__name__}: {exc}。"
                "已退回 .env 的单模型配置；修好该文件后刷新即可。"
            )
            return reg
        if not isinstance(raw, dict):
            reg.problems.append(f"{p.name} 的内容不是一个对象，已忽略。")
            return reg
        for item in raw.get("profiles") or []:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                prof = ModelProfile.from_dict(item)
                reg.profiles[prof.id] = prof
        reg.active = str(raw.get("active") or "").strip()
        roles = raw.get("roles") or {}
        if isinstance(roles, dict):
            reg.roles = {str(k): str(v) for k, v in roles.items() if str(v).strip()}
        return reg


# ---------------------------------------------------------------------------
# 按用途路由
# ---------------------------------------------------------------------------


class ModelRouter:
    """按 ``purpose`` 挑一个 ``LLMClient``，并缓存已构造的客户端。

    为什么要缓存：一次生成里有几十次调用，而 ``httpx.Client`` 每次新建都要重建
    连接池与 TLS 上下文。按用途分档只有几档，缓存住就够了。
    """

    def __init__(self, registry: ModelRegistry, base: LLMConfig) -> None:
        self.registry = registry
        self.base = base
        self._clients: dict[str, Any] = {}
        self.problems: list[str] = []

    def _build(self, profile: ModelProfile | None):
        from .llm import LLMClient

        if profile is None:
            return LLMClient(self.base)
        return LLMClient(profile.to_llm_config(self.base))

    def client_for(self, purpose: str):
        role = role_of(purpose)
        if role in self._clients:
            return self._clients[role]
        profile = self.registry.resolve(role) if self.registry.profiles else None
        if profile is not None and not profile.configured:
            self.problems.append(
                f"用途「{role}」指向的档案「{profile.label or profile.id}」没填全，已改用默认配置。"
            )
            profile = None
        client = self._build(profile)
        self._clients[role] = client
        return client

    @property
    def default(self):
        """默认客户端：优先用激活档案；没有档案时用 .env 的配置。"""
        key = "__default__"
        if key not in self._clients:
            self._clients[key] = self._build(self.registry.active_profile())
        return self._clients[key]

    def describe(self) -> dict:
        """每个用途实际会用哪个模型。报告的"模型调用"一节靠它说明白。"""
        out: dict[str, dict] = {}
        for role, desc in ROLES:
            profile = self.registry.resolve(role) if self.registry.profiles else None
            if profile is not None:
                out[role] = {"label": desc, "profile": profile.id,
                             "model": profile.model, "base_url": profile.base_url}
            else:
                out[role] = {"label": desc, "profile": "", "model": self.base.model,
                             "base_url": self.base.base_url}
        return out


def apply_override(settings: Any, model_id: str) -> str:
    """把 ``--model <档案id>`` 指定的档案用于**本次运行**。

    返回错误消息；空串表示成功。

    刻意只改这一次运行用的 ``settings``，**不写回 models.json**：
    "这次我想用另一个模型试试"和"以后都用这个"是两件事，
    前者不该悄悄改掉作者的长期配置。
    """
    mid = (model_id or "").strip()
    if not mid:
        return ""
    reg = getattr(settings, "models", None)
    if reg is None or not getattr(reg, "profiles", None):
        return "还没有配置任何模型档案（先在界面「设置 → 模型」里加一个，或用 `models add`）"
    prof = reg.profiles.get(mid)
    if prof is None:
        return f"没有名为「{mid}」的模型档案；已有：{'、'.join(reg.profiles) or '（无）'}"
    if not prof.configured:
        return f"档案「{prof.label or mid}」还没填全（需要 base_url、模型名与 API Key）"
    settings.llm = prof.to_llm_config(settings.llm)
    settings.llm_key_source = f"本次指定模型「{prof.label or prof.id}」"
    return ""


# ---------------------------------------------------------------------------
# 连通性测试
# ---------------------------------------------------------------------------

def test_profile(
    profile: ModelProfile,
    *,
    base: LLMConfig | None = None,
    prompt: str = "请只回复两个字：可用",
    timeout: float = 30.0,
) -> dict:
    """真实调用一次，确认这份档案能用。

    刻意**真发一次请求**而不是只检查字段是否填全：字段填全但 key 错、模型名不存在、
    额度用尽、网络不通，这四种情况只有真调一次才发现得了——而它们的表现都是
    "生成到一半才失败"，那时代价已经付出了。

    测试用的 prompt 与 max_tokens 都很小，成本可以忽略。
    """
    from .llm import LLMError, LLMClient

    cfg = profile.to_llm_config(base)
    if not cfg.api_key.strip() and profile.preset not in ("ollama", "vllm", "custom"):
        return {"ok": False, "error": "没有填 API Key", "seconds": 0.0, "reply": ""}
    client = LLMClient(replace(cfg, timeout=timeout))
    t0 = time.time()
    try:
        reply = client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=32,
            retries=0,
        )
        return {
            "ok": True,
            "error": "",
            "reply": (reply or "").strip()[:80],
            "seconds": round(time.time() - t0, 2),
            "model": cfg.model,
            "base_url": cfg.base_url,
        }
    except LLMError as exc:
        return {"ok": False, "error": str(exc)[:300], "reply": "",
                "seconds": round(time.time() - t0, 2), "model": cfg.model, "base_url": cfg.base_url}
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读结论
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "reply": "",
                "seconds": round(time.time() - t0, 2), "model": cfg.model, "base_url": cfg.base_url}
