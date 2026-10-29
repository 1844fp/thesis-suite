"""PaperQA2 本地 RAG：把知识库里的文献变成一个能回答问题的语料，并产出文献简报。

为什么是"套一层"而不是直接用 PaperQA2 的 API
--------------------------------------------
PaperQA2 的入口收的是**磁盘上的文档目录**（``Docs.aadd(path)``），而本站在
"资料获取 / 开题报告"阶段把文献存在 **SQLite** 里（``suite/rag.py``）。两者之间
必须有一步"把选中文献物化成一个临时语料目录"，这一步就是本模块存在的第一个理由。

第二个理由更关键：**凭据**。PaperQA2 走 litellm，只认进程环境变量；
本站是 BYOK，凭据在**请求头**里。两者直接混用有两个后果：
访客的 Key 会被写进 os.environ（并发请求互相覆盖、还可能被别的库读到），
或者访客的 Key 没传下去、PaperQA2 静默地去用了站点自己的凭据（用户不知情地花钱）。
所以这里把凭据**只放进子进程的 env**（``subprocess.Popen(env=...)``），
父进程的 os.environ 一个字节都不动。这与 ``suite/credentials.py`` 的既定防线一致。

第三：**依赖隔离**。PaperQA2 钉 numpy 2.5 / pydantic 2.14，本站跑的是 2.3 / 2.13，
同进程加载会让其中一方静默拿到错误版本。所以它住在自己的 ``libs-pqa`` 目录里，
以子进程 + JSONL 通信（``tools/pqa_bridge.py``）。**libs-pqa 不复制**——
本机 ``E:\\AI\\thesis-assistant\\libs-pqa`` 已有 1.19 GB 的完整安装，
复制一份既浪费磁盘又会有两份版本各自漂移，所以用路径引用（``PQA_LIBS``）。

中文与 CNKI PDF 的三个坑（都在 thesis-assistant 里实测过，这里照搬结论）
-----------------------------------------------------------------------
1. PaperQA2 的 ``maybe_is_text()`` 只接受每字符熵在 2.5~8.0 之间的文本。
   这个上限是按字母文字校准的（英文上界 log2(25)≈4.7），中文有约 1210 个相异字符，
   上界 log2(1210)≈10.24，所以**任何超过约 5000 字的中文论文都会被判为"不是文本"**。
   必须设 ``parsing.disable_doc_valid_check=True``。
2. 默认解析器 pypdf 读不了 CNKI 的 PDF（报 malformed MediaBox 且抽不出文字），
   必须装 ``paper-qa-pymupdf``。
3. ``fhlmi 1.0.7`` 与 ``aviary`` 组合下**智能体路径是坏的**
   （``_acompletion() takes 0 positional arguments but 1 was given``），
   所以走 ``Docs.aquery`` 手动 API，不经过 ``ToolSelector``。

语料怎么物化（这一层的设计决定）
--------------------------------
选中的文献按 ``<临时目录>/<doc_id>/<doc_id>.txt`` 落成纯文本：
**一篇文献一个子目录**，这样 PaperQA2 的引用里带的是文件名，
而文件名就是 doc_id —— 答案里的每条来源都能精确映射回知识库里的哪一篇文献，
不会出现"引用了一篇我已经删掉的文献"这种对不上的情况。
文件名里还写入题名与年份，因为 PaperQA2 会把文件名当 citation 用，
写进去以后答案里的引用直接可读。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import credentials as creds_mod
from . import rag

SUITE_ROOT = Path(__file__).resolve().parent.parent

#: PaperQA2 的安装目录。**默认引用 thesis-assistant 里已有的那份**（1.19 GB），
#: 不复制。想独立部署就用环境变量指向别处。
PQA_LIBS = Path(os.environ.get("SUITE_PQA_LIBS") or r"E:\AI\thesis-assistant\libs-pqa")

#: 子进程桥接脚本（本仓库自带，与 thesis-assistant 的那份是**同源不同用途**：
#: 那份只做问答，这份多了建索引与文献简报两个动作）。
BRIDGE = SUITE_ROOT / "tools" / "pqa_bridge.py"

#: 索引缓存、分词器缓存与语料目录。**语料要与它的签名文件分开放**：
#: 签名文件曾经放在语料目录里，结果 PaperQA2 把它当成第 4 篇文档索引了进去
#: （实测进度里出现过 "signature" 这一条）。签名是给我们自己看的，
#: 不能混进喂给模型的语料。
DATA_DIR = Path(os.environ.get("DSH_THESIS_DATA_DIR") or (SUITE_ROOT / "data"))
PQA_DIR = DATA_DIR / "paperqa"
INDEX_DIR = PQA_DIR / "index"
STAGE_DIR = PQA_DIR / "corpus"
STAGE_SIG = PQA_DIR / "corpus-signature.json"

#: 句子向量模型缓存。**指向 thesis-assistant 已经下好的那份**（91.9 MB，
#: bge-small-zh-v1.5）。本机访问不了 huggingface.co，重下一遍会直接失败，
#: 而 hf-mirror 也未必连得上——复用现成缓存是最稳的做法。
ST_HOME = Path(os.environ.get("SUITE_ST_HOME") or r"E:\AI\thesis-assistant\.tmp\st_home")

#: 默认的本地向量模型：中文、512 维、小（约 95 MB）。
DEFAULT_EMBED = "st-BAAI/bge-small-zh-v1.5"

#: 单次索引的文献数上限。每篇都要调模型做摘要+嵌入，几十篇就会很慢。
MAX_DOCS = int(os.environ.get("SUITE_PQA_MAX_DOCS") or 40)

#: 送进语料的单篇文献字符上限。
#:
#: 这个上限的旧值是 40000，依据是"整本专著塞进去一次提问的上下文会爆"——
#: **这个理由站不住**：PaperQA2 是先按块建索引、再按问题检索相关片段送进上下文，
#: 它从不把整篇文献塞进一次提问。所以真正的约束只是解析与嵌入的时间，不是上下文。
#: 40000 的实际后果是：一篇 47000 字的硕士论文会被截掉 7000 字，
#: 而"用完整文献生成简报"这件事就打了折扣。
#: 现在抬到 200000（与 fileinput 的单文件文本上限一致），只有极长的专著才会截断，
#: 且截断会**如实标注**（见 materialize 返回的 truncated）。
MAX_DOC_CHARS = int(os.environ.get("SUITE_PQA_MAX_DOC_CHARS") or 200000)


def available() -> bool:
    """PaperQA2 是否可用：桥接脚本 + 依赖目录 + 关键模块都在。"""
    return BRIDGE.is_file() and (PQA_LIBS / "paperqa").is_dir()


def status() -> dict:
    """给前端看的状态。**如实报告缺什么**，不要让用户点了才发现不能用。"""
    detail: dict = {
        "libs": str(PQA_LIBS),
        "bridge": str(BRIDGE),
        "index_dir": str(INDEX_DIR),
        "st_home": str(ST_HOME),
        "max_docs": MAX_DOCS,
    }
    if PQA_LIBS.is_dir():
        versions = sorted(p.name for p in PQA_LIBS.glob("paper_qa-*.dist-info"))
        detail["version"] = (versions[0].replace("paper_qa-", "").replace(".dist-info", "")
                            if versions else "?")
        detail["pymupdf_reader"] = (PQA_LIBS / "paperqa_pymupdf").is_dir()
        detail["local_embedding"] = (PQA_LIBS / "sentence_transformers").is_dir()
    detail["embedding_cached"] = any(ST_HOME.glob("models--*bge-small-zh*")) if ST_HOME.is_dir() else False
    detail["kbdocs"] = (rag.stats() or {}).get("documents", 0)

    ready = available()
    return {
        "ok": True,
        "available": ready,
        "detail": detail,
        # 前端据此决定是"可用"还是"给出安装指引"，所以缺什么必须逐项说清。
        "missing": ([] if ready else
                    [x for x, ok in (("tools/pqa_bridge.py", BRIDGE.is_file()),
                                     (f"PaperQA2 依赖目录 {PQA_LIBS}", (PQA_LIBS / "paperqa").is_dir()))
                     if not ok]),
        "note": (
            "PaperQA2 以子进程方式调用（numpy/pydantic 版本与主程序不同，不能同进程）。"
            "首次建索引会对每篇文献调用一次模型做摘要与向量化，"
            "之后复用缓存；索引只在选中的文献集合变化时重建。"
        ),
    }


# ---------------------------------------------------------------------------
# 一、凭据 → litellm 环境变量
# ---------------------------------------------------------------------------
#: 服务商 → (litellm 模型前缀, API Key 的环境变量名)。
#:
#: 为什么需要这张表：PaperQA2 用 litellm，而 litellm 从**环境变量**取密钥，
#: 变量名随服务商而变（``DEEPSEEK_API_KEY`` / ``OPENAI_API_KEY`` …）。
#: 认不出来的服务商统一走 ``openai/`` 前缀 + ``OPENAI_API_BASE`` 指向它的
#: OpenAI 兼容端点——这些国产服务商都提供兼容端点，这条路能覆盖绝大多数情况。
PROVIDER_LITELLM: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "zhipu": ("openai", "OPENAI_API_KEY"),
    "dashscope": ("openai", "OPENAI_API_KEY"),
    "moonshot": ("openai", "OPENAI_API_KEY"),
    "ollama": ("openai", "OPENAI_API_KEY"),
}

#: 站点自己的兜底凭据文件。**只在访客没填 Key 且站主显式开启时**才读。
#: 默认关闭：读别的项目的 .env 会打破 site/boot.py 已经建立的"绝不静默借密钥"防线
#: （站主若确实要这么做，得自己写一行环境变量承认这件事）。
LOCAL_KEY_ENV = "SUITE_PQA_LOCAL_KEY_FILE"


def _local_fallback() -> tuple[str, str, str, str]:
    """读站主显式指定的本地凭据文件，返回 ``(key, base, model, path)``。

    没设 ``SUITE_PQA_LOCAL_KEY_FILE`` 就返回空——**默认不借任何密钥**。
    这是本机自用场景的一个显式开关：本机跑的时候图省事，
    让 PaperQA2 直接用 thesis-assistant 已经配好的那把 Key，
    不必每次在页面上粘贴。它对公网部署无效（文件路径不存在就自动失效）。
    """
    raw = os.environ.get(LOCAL_KEY_ENV, "").strip()
    if not raw:
        return "", "", "", ""
    path = Path(raw)
    if not path.is_file():
        return "", "", "", ""
    key = base = model = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if name == "LLM_API_KEY" and value:
                key = value
            elif name == "LLM_BASE_URL" and value:
                base = value
            elif name == "LLM_MODEL" and value:
                model = value
    except OSError:
        return "", "", "", ""
    return key, base, model, str(path)


@dataclass
class PqaConfig:
    """一次 PaperQA2 调用需要的全部配置。"""

    api_key: str
    base_url: str
    model: str          # 原始模型名，如 deepseek-chat
    provider: str = "deepseek"
    source: str = "visitor"   # visitor / server / local-file
    note: str = ""

    @property
    def litellm_model(self) -> str:
        """litellm 认的模型串。已经带前缀的（``deepseek/xxx``）原样返回。"""
        model = (self.model or "").strip()
        if "/" in model:
            return model
        prefix = PROVIDER_LITELLM.get(self.provider, ("openai", "OPENAI_API_KEY"))[0]
        return f"{prefix}/{model}" if model else ""

    @property
    def key_env(self) -> str:
        return PROVIDER_LITELLM.get(self.provider, ("openai", "OPENAI_API_KEY"))[1]


def resolve_config(creds: creds_mod.Credentials, *, provider: str = "deepseek") -> PqaConfig:
    """把请求凭据解析成 PaperQA2 的配置，**优先级与站点其它功能完全一致**。

    访客填的 > 站点显式配置的 > 站主显式指定的本地文件（默认不读）> 没有。
    最后一种情况不报错，而是让调用方去告诉用户"这一步要 Key"——
    与 ``Chat._require`` 的措辞保持一致。
    """
    provider = (provider or "deepseek").strip() or "deepseek"

    key = (creds.api_key or "").strip()
    if key:
        base = (creds.base_url or "").strip()
        model = (creds.model or "").strip()
        if not model:
            model = _preset_model(provider)
        return PqaConfig(api_key=key, base_url=base, model=model, provider=provider,
                         source="visitor")

    server = creds_mod.server_credentials()
    if server.configured:
        return PqaConfig(
            api_key=server.api_key,
            base_url=(creds.base_url or "").strip() or server.base_url,
            model=(creds.model or "").strip() or server.model or _preset_model(provider),
            provider=provider, source="server",
            note="本次用的是本站配置的模型凭据，不是访客自己的。",
        )

    local_key, local_base, local_model, local_path = _local_fallback()
    if local_key:
        return PqaConfig(
            api_key=local_key,
            base_url=(creds.base_url or "").strip() or local_base,
            model=(creds.model or "").strip() or local_model or _preset_model(provider),
            provider=provider, source="local-file",
            note=f"本次用的是站主在本机显式指定的凭据文件（{local_path}）。",
        )

    return PqaConfig(api_key="", base_url="", model="", provider=provider, source="none")


def _preset_model(provider: str) -> str:
    try:
        presets = creds_mod.provider_presets()
    except Exception:  # noqa: BLE001 - 预设取不到就用 deepseek 的默认模型
        return "deepseek-chat"
    preset = presets.get(provider) or presets.get("deepseek") or {}
    return str(preset.get("model") or "deepseek-chat")


# ---------------------------------------------------------------------------
# 二、语料物化
# ---------------------------------------------------------------------------
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff（）()\-]+")


def _safe(name: str, limit: int = 60) -> str:
    text = _SAFE_NAME.sub("_", (name or "").strip())
    return text[:limit].strip("_") or "doc"


def materialize(doc_ids: list[str], *, rebuild: bool = False) -> dict:
    """把选中的知识库文献写成 PaperQA2 能读的语料目录。

    返回 ``{ok, dir, docs, chars, truncated, missing}``。

    **一篇文献一个子目录**：PaperQA2 会把文件名当 citation 塞进答案，
    子目录名用 doc_id，就能让答案里的每条来源精确映射回知识库。
    子目录里的文件名写成 ``题名（年份）.txt``，这是为了让 citation 直接可读——
    PaperQA2 取的是文件 stem，不是路径。

    选中的文献集合没变且不要求 rebuild 时**直接复用现有语料**：
    重新物化会让索引的签名变化，从而触发一次白花钱的重建。
    """
    doc_ids = [d for d in dict.fromkeys(doc_ids or []) if d]
    if not doc_ids:
        return {"ok": False, "error": "没有选中任何文献。", "docs": 0}

    if len(doc_ids) > MAX_DOCS:
        return {"ok": False,
                "error": f"一次最多索引 {MAX_DOCS} 篇文献（当前选中 {len(doc_ids)} 篇）。"
                         f"请减少选中数量后重试。"}

    signature = {"docs": sorted(doc_ids), "max_chars": MAX_DOC_CHARS}
    sig_file = STAGE_SIG
    if not rebuild and sig_file.is_file():
        try:
            if json.loads(sig_file.read_text(encoding="utf-8")) == signature:
                current = sorted(p.name for p in STAGE_DIR.glob("doc-*") if p.is_dir())
                if current == sorted(doc_ids):
                    return {"ok": True, "dir": str(STAGE_DIR), "docs": len(current),
                            "chars": _corpus_chars(), "reused": True,
                            "truncated": [], "missing": []}
        except Exception:  # noqa: BLE001 - 签名坏了就重建，不阻断
            pass

    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []
    truncated: list[dict] = []
    missing: list[str] = []
    for doc_id in doc_ids:
        doc = rag.get_document(doc_id)
        if not doc:
            missing.append(doc_id)
            continue
        text = (doc.get("text") or "").strip()
        if len(text) < 60:
            missing.append(doc_id)
            continue

        title = doc.get("title") or doc_id
        meta = doc.get("meta") or {}
        head = _citation_header(title, meta)
        body = text
        if len(body) > MAX_DOC_CHARS:
            truncated.append({"doc_id": doc_id, "title": title,
                              "kept": MAX_DOC_CHARS, "total": len(body)})
            body = body[:MAX_DOC_CHARS]

        sub = STAGE_DIR / doc_id
        sub.mkdir(parents=True, exist_ok=True)
        # 文件名 = PaperQA2 的 citation。题名 + 年份最有用，doc_id 放在子目录上。
        # **年份已经在题名里就不再补**：入库的题名常常自带年份
        # （例如「……研究（2024）」），无条件再拼一次会得到「（2024）（2024）」，
        # 而这个串会直接出现在答案的引用里（实测见过 "20262026 chunk 1"）。
        year = str(meta.get("year") or "").strip()
        label = title if (not year or year in title) else f"{title}（{year}）"
        (sub / f"{_safe(label)}.txt").write_text(head + body, encoding="utf-8")
        written.append({"doc_id": doc_id, "title": title, "chars": len(body)})

    if not written:
        return {"ok": False, "docs": 0,
                "error": "选中的文献在知识库里都取不到正文（可能已被删除或内容太短）。",
                "missing": missing}

    sig_file.write_text(json.dumps(signature, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "dir": str(STAGE_DIR), "docs": len(written),
            "chars": sum(w["chars"] for w in written),
            "written": written, "truncated": truncated, "missing": missing,
            "reused": False}


def _corpus_chars() -> int:
    """语料目录里正文的合计字符数（复用缓存时也要能报出这个数，否则前端会显示 0）。"""
    total = 0
    if STAGE_DIR.is_dir():
        for path in STAGE_DIR.rglob("*.txt"):
            try:
                total += len(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return total


def _citation_header(title: str, meta: dict) -> str:
    """在正文前面加一段书目信息。

    为什么要把书目写进正文而不是只放在文件名里：PaperQA2 对文本文件**不做**
     Crossref 查询（``use_doc_details`` 我们关掉了，因为 CNKI 论文查不到），
     citation 只能来自文件名；而卷期、DOI、被引这些信息文件名放不下。
    写进正文以后，模型在回答"这几篇的期刊分别是什么"时有真实依据可引。
    """
    bits = [f"题名：{title}"]
    for label, key in (("作者", "authors"), ("年份", "year"), ("期刊/来源", "venue"),
                       ("DOI", "doi"), ("文献等级", "tier"), ("ISSN", "issn"),
                       ("被引（OpenAlex）", "citations")):
        value = meta.get(key)
        if value not in (None, "", 0):
            bits.append(f"{label}：{value}")
    return "【文献信息】" + "；".join(bits) + "\n\n"


# ---------------------------------------------------------------------------
# 三、子进程调用
# ---------------------------------------------------------------------------
def _child_env(cfg: PqaConfig, *, hf_endpoint: str = "") -> dict:
    """构造子进程环境。**只碰这个 dict，绝不写 os.environ**（并发安全）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{PQA_LIBS}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 中文模型缓存直接复用现成的那份，避免去 huggingface.co 下载（本机不可达）。
    env["HF_ENDPOINT"] = hf_endpoint or os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    env["SENTENCE_TRANSFORMERS_HOME"] = str(ST_HOME)
    env["HF_HOME"] = str(PQA_DIR / "hf_home")
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["PQA_HOME"] = str(PQA_DIR / "home")

    # 凭据：只写进子进程的 env。同时把 litellm 可能认的几种变量都设上——
    # litellm 对不同服务商取不同的变量名，认不出来就会报"没有 API Key"，
    # 而那个错误信息完全指不到真正的原因。
    if cfg.api_key:
        env[cfg.key_env] = cfg.api_key
        env["OPENAI_API_KEY"] = cfg.api_key
        env["DEEPSEEK_API_KEY"] = cfg.api_key
    if cfg.base_url:
        env["OPENAI_API_BASE"] = cfg.base_url
        env["OPENAI_BASE_URL"] = cfg.base_url
        env["DEEPSEEK_API_BASE"] = cfg.base_url
    return env


def _request(cfg: PqaConfig, payload: dict) -> dict:
    out = dict(payload)
    out["llm"] = cfg.litellm_model
    out["corpus_dir"] = str(STAGE_DIR)
    out["index_dir"] = str(INDEX_DIR)
    return out


def run(cfg: PqaConfig, payload: dict, *, timeout: float = 900.0) -> list[dict]:
    """同步跑一次桥接，返回它吐出的全部 JSONL 事件。"""
    if not available():
        return [{"type": "error",
                 "message": "PaperQA2 未安装或路径不对。检查 SUITE_PQA_LIBS 指向的目录里有没有 paperqa。"}]
    if not cfg.api_key:
        return [{"type": "error",
                 "message": "这一步需要模型：请先在页面顶部填入你自己的 API Key。"}]

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    PQA_DIR.mkdir(parents=True, exist_ok=True)
    env = _child_env(cfg)
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), json.dumps(_request(cfg, payload), ensure_ascii=False)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    # stderr 必须在别的线程里持续读掉：PaperQA2 与 sentence-transformers 很吵，
    # 管道写满会把子进程卡死（它不写 stderr 就没法继续，我们不读就没人腾空间）。
    err_buf: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_buf.append(line)
            if len(err_buf) > 500:
                del err_buf[:250]

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()

    events: list[dict] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            events.append({"type": "error", "message": f"PaperQA2 超过 {timeout:.0f} 秒未返回，已中止。"})
        thread.join(timeout=2)

    if proc.returncode not in (0, None):
        tail = "".join(err_buf)[-600:].strip()
        events.append({"type": "error",
                       "message": f"PaperQA2 子进程异常退出（码 {proc.returncode}）"
                                  + (f"：{tail}" if tail else "")})
    return events


def stream(cfg: PqaConfig, payload: dict, *, timeout: float = 900.0):
    """流式跑一次桥接，逐条 yield 事件。用于 SSE。

    与 ``run`` 的区别只是"边收边给"：问答可能跑 10~40 秒，
    等它跑完再返回会让用户以为卡死了。索引与简报走 ``run`` 就够了。
    """
    if not available():
        yield {"type": "error", "message": "PaperQA2 未安装或路径不对。"}
        return
    if not cfg.api_key:
        yield {"type": "error",
               "message": "这一步需要模型：请先在页面顶部填入你自己的 API Key。"}
        return

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    env = _child_env(cfg)
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), json.dumps(_request(cfg, payload), ensure_ascii=False)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    err_buf: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_buf.append(line)
            if len(err_buf) > 500:
                del err_buf[:250]

    threading.Thread(target=drain, daemon=True).start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None):
            tail = "".join(err_buf)[-600:].strip()
            yield {"type": "error",
                   "message": f"PaperQA2 子进程异常退出（码 {proc.returncode}）"
                              + (f"：{tail}" if tail else "")}


# ---------------------------------------------------------------------------
# 四、高层动作
# ---------------------------------------------------------------------------
def ask(cfg: PqaConfig, question: str, doc_ids: list[str], *,
        rebuild: bool = False, timeout: float = 900.0) -> dict:
    """对选中的文献提问，返回 ``{ok, answer, sources, seconds, cost, corpus}``。"""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "请填写要问的问题。"}

    corpus = materialize(doc_ids, rebuild=rebuild)
    if not corpus.get("ok"):
        return corpus

    events = run(cfg, {"action": "ask", "question": question, "rebuild": rebuild},
                 timeout=timeout)
    return _collect(events, corpus)


def brief(cfg: PqaConfig, doc_ids: list[str], *, focus: str = "",
          mode: str = "brief", rebuild: bool = False, timeout: float = 900.0) -> dict:
    """对选中的文献做结构化简报。``mode`` 为 ``brief``（单篇）或 ``digest``（多篇）。"""
    if mode not in {"brief", "digest"}:
        return {"ok": False, "error": f"不支持的简报模式 {mode!r}。"}

    corpus = materialize(doc_ids, rebuild=rebuild)
    if not corpus.get("ok"):
        return corpus

    events = run(cfg, {"action": "brief", "mode": mode, "focus": (focus or "").strip(),
                       "rebuild": rebuild}, timeout=timeout)
    return _collect(events, corpus)


def _collect(events: list[dict], corpus: dict) -> dict:
    """把事件流收成一次结果。**错误原样返回，不吞**。"""
    errors = [e.get("message") for e in events if e.get("type") == "error"]
    progress = [e.get("message") for e in events if e.get("type") == "progress"]
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    sources = next((e.get("sources") for e in reversed(events) if e.get("type") == "sources"), [])

    if errors:
        return {"ok": False, "error": errors[-1], "progress": progress,
                "corpus": _corpus_view(corpus)}
    if not done:
        return {"ok": False, "error": "PaperQA2 没有返回结果。", "progress": progress,
                "corpus": _corpus_view(corpus)}

    return {
        "ok": True,
        "answer": done.get("answer") or "",
        # 简报模式下 ``answer`` 是空的，正文在 ``brief`` 里（逐节结构）。
        # **这里必须原样透出**：漏掉它会让 /api/pqa/brief 返回一个没有 brief 键的
        # 响应，前端只能退回渲染空字符串——表现出来就是"点了出简报，什么都没发生"，
        # 而它其实已经花了八次模型调用。这个字段曾经就是漏的（前端靠两种口径兜住了，
        # 但那种兼容不该是常态）。
        "brief": done.get("brief") or None,
        "sources": sources or [],
        "seconds": done.get("seconds"),
        "cost": done.get("cost"),
        "reused": done.get("reused"),
        "progress": progress,
        "corpus": _corpus_view(corpus),
    }


def _corpus_view(corpus: dict) -> dict:
    """把语料信息里对用户有意义的部分挑出来（不暴露服务器绝对路径之外的东西）。"""
    return {
        "docs": corpus.get("docs", 0),
        "chars": corpus.get("chars", 0),
        "reused": corpus.get("reused", False),
        "truncated": corpus.get("truncated") or [],
        "missing": corpus.get("missing") or [],
        "dir": corpus.get("dir", ""),
    }


def clear_index() -> dict:
    """删掉索引缓存（不清语料）。索引与嵌入模型不匹配时必须重建，这是入口。"""
    removed = []
    for path in (INDEX_DIR,):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    return {"ok": True, "removed": removed,
            "note": "索引已清空，下次提问会重新建索引（对每篇文献调用一次模型）。"}


def stats() -> dict:
    """索引与语料现状，供前端显示"现在有没有建好"。"""
    out: dict = {"ok": True, "index_dir": str(INDEX_DIR), "corpus_dir": str(STAGE_DIR)}
    sig = STAGE_SIG
    if sig.is_file():
        try:
            data = json.loads(sig.read_text(encoding="utf-8"))
            out["corpus_docs"] = len(data.get("docs") or [])
        except Exception:  # noqa: BLE001
            pass
    pkl = INDEX_DIR / "docs.pkl"
    if pkl.is_file():
        out["indexed"] = True
        out["index_bytes"] = pkl.stat().st_size
        out["index_mtime"] = time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(pkl.stat().st_mtime))
    else:
        out["indexed"] = False
    return out
