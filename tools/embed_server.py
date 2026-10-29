"""语义向量服务（常驻子进程）：把句子编码成向量，供知识库做混合检索。

为什么必须另起进程
------------------
向量模型（`sentence-transformers` + torch）住在 ``E:\\AI\\thesis-assistant\\libs-pqa`` 里，
那一套钉的是 numpy 2.5 / pydantic 2.14，而站点主进程跑的是 numpy 2.3 / pydantic 2.13。
**在同一个解释器里同时加载两者，必有一方静默拿到错的版本**——这正是 PaperQA2
也要走子进程的同一个原因（见 ``suite/paperqa.py``）。

代价是冷启动：``import torch`` 约 33 秒、加载模型约 7 秒。所以这个进程是**常驻**的：
启动一次，之后每条查询只花 ~80 毫秒。一次性子进程的方案会让每次检索多等 40 秒，
那是不可接受的。

协议（JSONL，一行一次请求/响应）
--------------------------------
stdin::

    {"texts": ["第一段", "第二段"]}      # 编码
    {"exit": true}                        # 让进程退出

stdout::

    {"ok": true, "ready": true, "dim": 512, "model": "BAAI/bge-small-zh-v1.5"}
    {"ok": true, "vectors": [[...], [...]], "dim": 512}
    {"ok": false, "error": "..."}

stderr 随便打（模型加载会往 stderr 刷进度条），调用方会持续读掉，避免管道写满卡死。
"""

from __future__ import annotations

import json
import os
import sys

#: 模型名。bge-small-zh-v1.5：中文、512 维、约 91 MB，本机已缓存。
#: 选小模型是有意的——知识库检索是**高频**路径，每次查询都要跑一次编码，
#: 大模型（bge-m3 / bge-large）会把单次检索从 80ms 拉到 300ms 以上，
#: 而在这批语料上召回差异很小。
DEFAULT_MODEL = os.environ.get("SUITE_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

#: 单条文本的截断长度。bge 的上下文是 512 token（中文约 500~800 字），
#: 超长会由模型内部截断；这里先切一刀，避免把整章正文丢进去白算。
MAX_CHARS = int(os.environ.get("SUITE_EMBED_MAX_CHARS", "2000"))


def emit(**payload) -> None:
    """往 stdout 打一行 JSON。**这是唯一的输出通道**，别的 print 会污染协议。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    # 这些必须在 import torch / transformers 之前设好：
    # 本机访问不了 huggingface.co，靠镜像；模型缓存目录复用 thesis-assistant 已下好的那份。
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # noqa: BLE001 - 缺依赖是**正常降级路径**，不是崩溃
        emit(ok=False, error=f"无法导入 sentence_transformers：{type(exc).__name__}: {exc}")
        return 1

    try:
        model = SentenceTransformer(DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001 - 模型没缓存又下不来时走这里
        emit(ok=False, error=f"加载模型失败：{type(exc).__name__}: {exc}")
        return 1

    try:
        dim = int(model.get_sentence_embedding_dimension() or 0)
    except Exception:  # noqa: BLE001
        dim = 0

    emit(ok=True, ready=True, dim=dim, model=DEFAULT_MODEL)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            emit(ok=False, error=f"请求不是合法 JSON：{exc}")
            continue
        if req.get("exit"):
            break

        texts = req.get("texts") or []
        if not isinstance(texts, list) or not texts:
            emit(ok=True, vectors=[], dim=dim)
            continue

        try:
            cleaned = [str(t)[:MAX_CHARS] for t in texts]
            vectors = model.encode(
                cleaned,
                normalize_embeddings=True,   # 归一化后余弦 = 点积，省一次除法
                batch_size=32,
                show_progress_bar=False,
            )
            emit(ok=True, vectors=vectors.tolist(), dim=dim)
        except Exception as exc:  # noqa: BLE001 - 单次编码失败不该让常驻进程退出
            emit(ok=False, error=f"编码失败：{type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
