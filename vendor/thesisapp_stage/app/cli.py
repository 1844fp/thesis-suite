"""命令行入口。

设计原则：**每条命令的输出都要能直接用于判断"下一步该做什么"。**
错误消息必须包含可执行的指引（哪个服务没起、该跑哪条命令），
而不是抛一段调用栈。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import PROJECTS_DIR, Settings, ensure_dirs, load_settings
from .framework.blackboard import Blackboard


def _print(obj, *, indent: int = 2) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=indent, default=str))


def _sev_mark(sev: str) -> str:
    return {"blocker": "■", "major": "▲", "minor": "·", "info": " "}.get(sev, " ")


def cmd_agents(args, settings: Settings) -> int:
    from .runtime import build_runtime

    rt = build_runtime(settings)
    if args.json:
        _print(rt.describe())
        return 0

    print("已注册智能体：")
    for a in rt.registry.describe():
        tag = "模型" if a["uses_model"] else "本地"
        print(f"  {a['name']:<24} [{tag:<2}] {a['description']}")
    from .framework.registry import registry_snapshot

    snap = registry_snapshot()
    print(f"\n检查器 {len(snap['verifiers'])} 个：")
    for v in snap["verifiers"]:
        tag = "模型" if v["needs_llm"] else "本地"
        print(f"  {v['name']:<28} [{tag}] {v['description']}")
    print(f"\n修复算子 {len(snap['fixers'])} 个：")
    for f in snap["fixers"]:
        print(f"  {f['name']:<28} {f['description']}")

    if rt.skipped:
        print("\n未注册的可选能力（不影响其余功能）：")
        for k, v in rt.skipped.items():
            print(f"  {k}: {v}")
    return 0


def cmd_health(args, settings: Settings) -> int:
    from .adapters.rag import RAGAdapter
    from .adapters.reducer import ReducerAdapter

    rows = [
        RAGAdapter(settings.peers.rag_base, timeout=10).health("/api/status"),
        ReducerAdapter(settings.peers.reducer_base, timeout=10).health(),
    ]
    if args.json:
        _print([r.as_dict() for r in rows])
    else:
        print("对端服务：")
        for r in rows:
            mark = "✅" if r.reachable else "❌"
            extra = f"  {r.detail[:90]}" if not r.reachable else ""
            print(f"  {mark} {r.name}  {r.base_url}")
            if extra:
                print(f"     {extra.strip()}")
    ok = all(r.reachable for r in rows)
    if not ok:
        print("\n提示：不可达的服务只影响对应能力，其余功能照常可用。")
        print("      启动命令：E:\\AI\\thesis-assistant\\run.cmd 与 E:\\AI\\aigc-reducer\\run.cmd")
    llm_ok = bool(settings.llm.api_key.strip())
    print(f"\n生成模型：{'✅ 已配置' if llm_ok else '❌ 未配置 API Key'}  {settings.llm.model} @ {settings.llm.base_url}")
    return 0 if ok else 1


def cmd_new(args, settings: Settings) -> int:
    from .pipeline import Runner

    runner = Runner()
    meta: dict = {}
    if args.topic:
        meta["writing_brief"] = {"topic": args.topic}
    if args.query:
        meta["retrieve_queries"] = list(args.query)
    if args.root:
        meta["gather_roots"] = list(args.root)

    if args.file:
        board = runner.create_from_file(args.file, title=args.title or "", meta=meta)
    else:
        text = Path(args.text).read_text(encoding="utf-8") if args.text else ""
        board = runner.create_project(title=args.title or "未命名项目", text=text, meta=meta)

    print(f"已建立项目：{board.project.id}")
    print(f"  标题  ：{board.project.title}")
    print(f"  稿件  ：{len(board.draft_text)} 字，v{board.draft.version if board.draft else 0}")
    if board.project.meta.get("intake_warnings"):
        print("  收稿警告：")
        for w in board.project.meta["intake_warnings"]:
            print(f"    - {w}")
    return 0


def cmd_projects(args, settings: Settings) -> int:
    rows = Blackboard.list_projects(PROJECTS_DIR)
    if args.json:
        _print(rows)
        return 0
    if not rows:
        print("还没有项目。用 `new` 建立一个。")
        return 0
    print(f"{'项目 id':<24} {'阶段':<8} {'字数':>7} {'问题':>10}  标题")
    for r in rows:
        print(
            f"{r['id']:<24} {r['stage']:<8} {r['chars']:>7} "
            f"{r['issues_open']:>4}/{r['issues_total']:<5} {r['title']}"
        )
    return 0


def cmd_status(args, settings: Settings) -> int:
    board = Blackboard.load(PROJECTS_DIR, args.project)
    p = board.project
    if args.json:
        _print(
            {
                "project": p.id,
                "title": p.title,
                "stage": p.stage,
                "severity": board.severity_counts(),
                "artifacts": {
                    kind: {"versions": len(board.versions(kind)), "head": board.head(kind).id if board.head(kind) else ""}
                    for kind in sorted({a.kind for a in p.artifacts.values()})
                },
                "history_tail": p.history[-12:],
                "ledgerless": True,
            }
        )
        return 0

    print(f"项目 {p.id} · {p.title}")
    print(f"  阶段：{p.stage}    稿件：{len(board.draft_text)} 字 v{board.draft.version if board.draft else 0}")
    print(f"  工件：")
    for kind in sorted({a.kind for a in p.artifacts.values()}):
        vs = board.versions(kind)
        print(f"    {kind:<20} {len(vs)} 个版本，当前 v{vs[-1].version}")
    counts = board.severity_counts()
    print(f"  问题：致命 {counts['blocker']} · 重要 {counts['major']} · 轻微 {counts['minor']} "
          f"· 提示 {counts['info']} · 已修复 {counts['fixed']} · 待人工 {counts['needs_author']}")
    print("\n  最近动作：")
    for h in p.history[-10:]:
        ts = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
        print(f"    {ts} [{h['stage']}] {h['actor']}: {h['action']} {h['detail'][:70]}")
    return 0


def cmd_issues(args, settings: Settings) -> int:
    board = Blackboard.load(PROJECTS_DIR, args.project)
    issues = list(board.project.issues.values())
    if args.open_only:
        issues = [i for i in issues if i.status == "open"]
    if args.status:
        issues = [i for i in issues if i.status == args.status]
    if args.fixable:
        issues = [i for i in issues if i.fixable]
    issues.sort(key=lambda i: (-i.rank, i.kind))

    if args.json:
        _print([i.as_dict() for i in issues])
        return 0
    if not issues:
        print("没有符合条件的问题。")
        return 0
    print(f"共 {len(issues)} 条：\n")
    for i in issues:
        fix = f" 可修({i.fix_op})" if i.fixable else ""
        print(f"{_sev_mark(i.severity.value)} [{i.severity.value:<7}] {i.kind}{fix}  ({i.status})")
        print(f"    {i.message}")
        if i.evidence:
            print(f"    证据：{i.evidence[:110]}")
        if i.fix_hint:
            print(f"    建议：{i.fix_hint[:130]}")
        if i.location:
            print(f"    定位：{json.dumps(i.location, ensure_ascii=False)[:110]}")
        print()
    return 0


def cmd_run(args, settings: Settings) -> int:
    from .pipeline import Runner

    stages = args.stages or None
    runner = Runner()
    last = [""]

    def progress(msg: str, ratio: float) -> None:
        if args.quiet:
            return
        if ratio >= 0:
            pct = f"{ratio * 100:5.1f}%"
        else:
            pct = "     "
        if msg != last[0]:
            print(f"  {pct} {msg}", file=sys.stderr)
            last[0] = msg

    result = runner.run(
        args.project,
        stages=stages,
        max_rounds=args.max_rounds,
        include_llm_verify=not args.no_llm_verify,
        progress=progress,
        dry_run=args.dry_run,
    )
    if args.json:
        _print(result.as_dict())
        return 0 if not result.failures else 1

    print(f"\n流程结束：{result.seconds:.1f}s" + ("（dry-run，未修改稿件）" if result.dry_run else ""))
    for s in result.stages:
        if not s.ran:
            print(f"  ⏭ {s.stage:<9} 跳过：{s.reason}")
            continue
        mark = "✅" if s.ok else "❌"
        print(f"  {mark} {s.stage:<9} {s.summary}")
    if result.self_correction:
        sc = result.self_correction
        print(
            f"\n自我更正：{sc['rounds']} 轮，收敛={sc['converged']}，"
            f"剩余致命 {sc['remaining_blockers']} · 重要 {sc['remaining_majors']} · "
            f"待人工 {sc['needs_author']}"
        )
    led = result.ledger
    if led:
        print(f"模型调用：{led['calls']} 次，失败 {led['failures']}，耗时 {led['seconds']}s")
    c = result.severity
    print(f"问题统计：致命 {c['blocker']} · 重要 {c['major']} · 轻微 {c['minor']} · 提示 {c['info']} "
          f"· 已修复 {c['fixed']} · 待人工 {c['needs_author']}")
    if result.failures:
        print(f"\n有 {len(result.failures)} 个阶段失败，用 `issues -p {args.project}` 查看详情。")
        return 1
    return 0


def cmd_search(args, settings: Settings) -> int:
    from .adapters.base import PeerError
    from .adapters.rag import RAGAdapter

    rag = RAGAdapter(settings.peers.rag_base, timeout=settings.peers.timeout)
    try:
        hits = rag.search(args.query, k=args.k, rerank=not args.no_rerank)
    except PeerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.json:
        _print([h.as_dict() for h in hits])
        return 0
    if not hits:
        print("没有命中。试试换关键词，或先用 `scan` 看看知识库里有什么。")
        return 0
    for h in hits:
        print(f"\n[{h.index}] {h.citation}   分数 {h.score:.3f}")
        print(f"    {h.text[:300]}")
    return 0


def cmd_scan(args, settings: Settings) -> int:
    from .adapters.research import LocalIntake
    from .config import INBOX_DIR

    roots = [INBOX_DIR, Path(r"E:\AI\thesis-assistant\inbox")] + [Path(r) for r in (args.root or [])]
    intake = LocalIntake(roots)
    items = intake.scan()
    summary = intake.summary(items)
    if args.json:
        _print({"summary": summary, "items": [i.as_dict() for i in items]})
        return 0
    print(f"扫描目录：{summary['roots']}")
    print(f"共 {summary['total']} 个文件：可入库 {summary['ingestible']}，跳过 {summary['skipped']}")
    print(f"按扩展名：{summary['by_ext']}\n")
    for it in items:
        mark = "✅" if it.ingestible else "⏭"
        who = f" · {it.author}" if it.author else ""
        print(f"  {mark} {it.filename}{who}")
        if it.parse_note:
            print(f"      {it.parse_note}")
    return 0


def cmd_ask(args, settings: Settings) -> int:
    from .adapters.base import PeerError
    from .adapters.rag import RAGAdapter

    rag = RAGAdapter(settings.peers.rag_base, timeout=settings.peers.timeout)
    try:
        ans = rag.ask(args.question, k=args.k)
    except PeerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if ans.error:
        print(f"错误：{ans.error}", file=sys.stderr)
        return 1
    if ans.sources and not args.quiet:
        print("参考片段：")
        for s in ans.sources:
            print(f"  [{s.get('index')}] {s.get('citation')}")
        print()
    print(ans.text)
    return 0


def cmd_thesis(args, settings: Settings) -> int:
    """一键生成：开题报告 → 硕士论文初稿（.docx + 生成报告）。"""
    from .thesis import DEFAULT_STAGES, ThesisRequest, generate_thesis

    req = ThesisRequest(
        proposal_path=args.proposal or "",
        title=args.title or "",
        author=args.author or "",
        topic=args.topic or "",
        discipline=args.discipline or "",
        genre=args.genre or "",
        requirement=args.requirement or "",
        degree=args.degree or "硕士",
        word_target=int(args.words or 0),
        sections=list(args.section or []),
        chapters=list(args.chapter or []),
        skip_chapters=list(args.skip_chapter or []),
        out_path=args.out or "",
        report_path=args.report or "",
        stages=tuple(args.stages) if args.stages else DEFAULT_STAGES,
        export=not args.no_export,
        llm_verify=bool(args.llm_verify),
        max_rounds=int(args.max_rounds or 2),
        precheck=not args.no_precheck,
        force=bool(args.force),
        model=args.model or "",
    )
    if args.with_dedup:
        req.stages = tuple(dict.fromkeys((*req.stages, "dedup")))
    if args.with_format:
        req.stages = tuple(dict.fromkeys((*req.stages, "format")))
    if args.with_gather:
        req.stages = tuple(dict.fromkeys(("gather", *req.stages)))

    last = [""]

    def progress(msg: str, ratio: float) -> None:
        if args.quiet:
            return
        if msg == last[0]:
            return
        last[0] = msg
        pct = f"{ratio * 100:5.1f}%" if ratio >= 0 else "     "
        print(f"  {pct} {msg}", file=sys.stderr)

    try:
        result = generate_thesis(req, settings=settings, progress=progress)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        _print(result.as_dict())
        return 0 if result.ok else 1

    if result.blocked:
        print("\n⛔ 已停止：知识库预检未通过（**没有产生任何模型费用**）\n")
        print(f"  {result.blocked}\n")
        pre = result.precheck or {}
        print(f"  题目关键词：{'、'.join(pre.get('keywords') or []) or '（未抽取到）'}")
        for q in pre.get("queries") or []:
            print(f"    · 「{q.get('query')}」命中 {q.get('hits')} 条，相关 {q.get('relevant')} 条")
        print("\n下一步：")
        for s in result.proposals:
            print(f"  · {s}")
        return 1

    print(f"\n完成：{result.seconds:.1f}s  项目 {result.project_id}")
    print(f"  题目：{result.title}")
    if result.precheck:
        pre = result.precheck
        print(f"  预检：{pre.get('verdict')}　相关 {pre.get('relevant')}/{pre.get('total')}"
              f"（占比 {pre.get('ratio')}）")
    words = sum(result.chapter_words.values())
    print(f"  正文：{words} 字（{len(result.chapter_words)} 章）")
    for s in result.stage_summaries:
        if not s["ran"]:
            print(f"  ⏭ {s['stage']:<9} 跳过：{s['reason']}")
        else:
            print(f"  {'✅' if s['ok'] else '❌'} {s['stage']:<9} {s['summary'][:160]}")
    c = result.severity
    print(
        f"\n问题统计：致命 {c.get('blocker', 0)} · 重要 {c.get('major', 0)} · "
        f"轻微 {c.get('minor', 0)} · 提示 {c.get('info', 0)} · 待人工 {c.get('needs_author', 0)}"
    )
    led = result.ledger or {}
    if led:
        print(f"模型调用：{led.get('calls', 0)} 次，失败 {led.get('failures', 0)}，耗时 {led.get('seconds', 0)}s")
        # 按用途分模型之后，"哪个模型被调了多少次"直接决定账单，
        # 也回答"正文到底是不是用了那个强模型"。
        by_model = led.get("by_model") or {}
        if len(by_model) > 1:
            for name, v in sorted(by_model.items(), key=lambda kv: -kv[1]["calls"]):
                print(f"    · {name}：{v['calls']} 次，{v['seconds']}s")
    ex = result.export or {}
    if ex.get("path"):
        mark = "✅" if ex.get("ok") else "⚠"
        print(f"\n{mark} 稿件：{ex['path']}")
        print(f"    {ex.get('paragraphs', 0)} 段 · 标题 {ex.get('headings', 0)} 个 · "
              f"文献 {ex.get('references', 0)} 条 · 待办标记 {ex.get('pending_marks', 0)} 处")
    if result.report_path:
        print(f"📋 生成报告：{result.report_path}")
    if result.proposals:
        print("\n下一步：")
        for s in result.proposals:
            print(f"  · {s}")
    return 0 if result.ok else 1


def cmd_propose(args, settings: Settings) -> int:
    """只解析开题报告：先看清楚系统读到了什么，再决定要不要生成。"""
    from .parsing.proposal import parse_proposal

    try:
        brief = parse_proposal(args.proposal, title_hint=args.title or "")
    except Exception as exc:  # noqa: BLE001 - 解析失败要给可读原因
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print(brief.as_dict())
        return 0

    print(f"题目：{brief.title or '（未识别）'}")
    print(f"作者：{brief.author or '（未识别）'}    案例对象：{brief.case or '（未识别）'}")
    print(f"\n章节结构（{len(brief.chapters)} 章）：")
    for i, c in enumerate(brief.chapters, 1):
        kids = c.get("children") or []
        print(f"  {i}. {c['title']}　［{len(kids)} 个小节］")
        for k in kids:
            print(f"       - {k}")
    print(f"\n研究方法：{'、'.join(brief.methods) or '（未识别）'}")
    print(f"开题声明的创新点：{len(brief.innovations)} 条（只作记录，不参与生成）")
    for x in brief.innovations:
        print(f"  · {x[:80]}")
    print(f"参考文献：{brief.reference_count} 条")
    print(f"识别到的栏目：{'、'.join(brief.regions_found) or '（无）'}")
    if brief.regions_missing:
        print(f"⚠ 未识别的栏目：{'、'.join(brief.regions_missing)}")
    if brief.warnings:
        print("\n提示：")
        for w in brief.warnings:
            print(f"  ! {w}")
    return 0


def cmd_kb(args, settings: Settings) -> int:
    """知识库：查库、试查、预检、投料入库。"""
    from .kb import INGESTIBLE_SUFFIXES, add_files, ingest, kb_status, list_docs, precheck, search

    action = args.action or "status"

    if action == "status":
        st = kb_status(settings)
        if args.json:
            _print(st.as_dict())
            return 0 if st.reachable else 1
        mark = "✅" if st.reachable else "❌"
        print(f"{mark} 知识库服务：{st.base_url}")
        if st.reachable:
            print(f"   馆藏：{st.docs} 篇文献 / {st.chunks} 个片段")
        else:
            print(f"   连不上：{st.error}")
            print("   启动：E:\\AI\\thesis-assistant\\run.cmd")
        print(f"   收料目录：{st.inbox}（{st.inbox_files} 个源文件）")
        print("   提示：入库后的文献会留在收料目录里；`kb list` 看馆藏，`kb add` 投新资料。")
        return 0 if st.reachable else 1

    if action == "list":
        try:
            docs = list_docs(settings)
        except Exception as exc:  # noqa: BLE001
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        if args.json:
            _print(docs)
            return 0
        print(f"知识库共 {len(docs)} 篇：\n")
        for d in docs:
            print(f"  {d.get('doc_id','')}  {d.get('year') or '----'}  "
                  f"{(d.get('author') or '佚名'):<8} {d.get('n_chunks',0):>4} 块  {d.get('title','')}")
        return 0

    if action == "search":
        if not args.query:
            print("错误：kb search 需要一个查询词", file=sys.stderr)
            return 2
        try:
            hits = search(args.query, k=args.k, settings=settings, rerank=not args.no_rerank)
        except Exception as exc:  # noqa: BLE001
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        if args.json:
            _print([h.as_dict() for h in hits])
            return 0
        if not hits:
            print("没有命中。")
            return 0
        for h in hits:
            print(f"\n[{h.index}] {h.citation}   分数 {h.score:.3f}")
            print(f"    {h.text[:280]}")
        return 0

    if action == "check":
        topic = args.topic or args.query or ""
        if not topic:
            print("错误：kb check 需要 --topic \"你的论文题目\"", file=sys.stderr)
            return 2
        r = precheck(topic, requirement=args.requirement or "", k=args.k, settings=settings)
        if args.json:
            _print(r.as_dict())
            return 0 if r.usable else 1
        print(f"题目：{topic}")
        print(f"题目关键词：{'、'.join(r.keywords.phrases) or '（未抽取到）'}\n")
        for q in r.queries:
            print(f"  「{q.query}」命中 {q.hits} 条，相关 {q.relevant} 条 [{q.level}]")
            for t in q.titles[:5]:
                print(f"      - {t}")
        print()
        print(f"判定：{r.verdict}　相关 {r.relevant}/{r.total}（占比 {r.ratio:.2f}）")
        print(f"结论：{r.suggestion()}")
        return 0 if r.usable else 1

    if action == "add":
        paths = list(args.files or [])
        for d in args.dir or []:
            p = Path(d)
            if not p.is_dir():
                print(f"警告：{d} 不是目录，已跳过", file=sys.stderr)
                continue
            paths.extend(
                str(x) for x in sorted(p.rglob("*"))
                if x.is_file() and x.suffix.lower() in INGESTIBLE_SUFFIXES
            )
        if not paths:
            print(f"错误：没有要投料的文件。支持 {'、'.join(sorted(INGESTIBLE_SUFFIXES))}", file=sys.stderr)
            return 2
        res = add_files(paths, settings=settings, ingest=not args.no_ingest, force=args.force)
        if args.json:
            _print(res)
            return 0
        print(f"收料目录：{res['inbox']}")
        print(f"已投料 {len(res['copied'])} 个文件：")
        for c in res["copied"]:
            print(f"  ✅ {Path(c['path']).name}（{c['bytes']} 字节）")
        if res["skipped"]:
            print(f"跳过 {len(res['skipped'])} 个：")
            for s in res["skipped"]:
                print(f"  ⏭ {Path(s['path']).name}：{s['reason']}")
        if res["ingested"]:
            ok = sum(1 for x in res["ingested"] if x.get("ok"))
            print(f"入库完成：{ok}/{len(res['ingested'])} 成功")
            for x in res["ingested"][:20]:
                mark = "✅" if x.get("ok") else "❌"
                print(f"  {mark} {x.get('file') or x.get('path') or ''} {x.get('message') or x.get('error') or ''}".rstrip())
        if res["ingest_error"]:
            print(f"⚠ 入库未完成：{res['ingest_error']}", file=sys.stderr)
            return 1
        return 0

    if action == "ingest":
        try:
            results = ingest(settings, force=args.force)
        except Exception as exc:  # noqa: BLE001
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        if args.json:
            _print(results)
            return 0
        ok = sum(1 for x in results if x.get("ok"))
        print(f"入库完成：{ok}/{len(results)} 成功")
        for x in results[:30]:
            mark = "✅" if x.get("ok") else "❌"
            print(f"  {mark} {x.get('file') or x.get('path') or ''} {x.get('message') or x.get('error') or ''}".rstrip())
        return 0

    print(f"错误：未知操作 {action}", file=sys.stderr)
    return 2


def cmd_rewrite(args, settings: Settings) -> int:
    """按作者的要求改写指定章节或原文片段。

    改完必须重新定稿（角标要从证据池编号改回连续编号），所以这条命令
    连着跑 ``writer:rewrite`` → ``writer:references``，最后可选重新导出。
    """
    import json as _json

    from .framework.artifact import Stage
    from .framework.state import Pipeline
    from .pipeline import Runner
    from .runtime import build_runtime

    targets: list[dict] = []
    if args.targets:
        try:
            parsed = _json.loads(args.targets)
            if isinstance(parsed, dict):
                parsed = [parsed]
            targets.extend(t for t in parsed if isinstance(t, dict))
        except Exception as exc:  # noqa: BLE001
            print(f"错误：--targets 不是合法 JSON：{exc}", file=sys.stderr)
            return 2
    if args.section or args.find:
        instruction = (args.instruction or "").strip()
        if not instruction:
            print("错误：用 --section/--find 时必须给 --instruction「改写要求」", file=sys.stderr)
            return 2
        targets.append({"section": args.section or "", "find": args.find or "", "instruction": instruction})
    if not targets:
        print(
            "错误：没有改写目标。用法示例：\n"
            "  run_python.cmd -m app.cli rewrite -p <项目id> --section 第3章 "
            "--instruction \"把机制分析补实，用检索到的文献支撑\"\n"
            "  run_python.cmd -m app.cli rewrite -p <项目id> --find \"原文里的一句话\" "
            "--instruction \"改写这一段\"",
            file=sys.stderr,
        )
        return 2

    rt = build_runtime(settings)
    runner = Runner(runtime=rt)
    board = runner.load(args.project)
    meta = board.project.meta
    meta["rewrite_targets"] = targets
    board.save()

    ctx = rt.new_context(board, stage=Stage.DRAFT.value)
    if args.model:
        from .models import apply_override

        problem = apply_override(ctx.settings, args.model)
        if problem:
            print(f"错误：{problem}", file=sys.stderr)
            return 2
        ctx.llm = ctx.router.default if ctx.router is not None else ctx.llm
        print(f"本次改写使用模型档案「{args.model}」")
    pipe = Pipeline(rt.registry, ctx)

    def progress(msg: str, ratio: float) -> None:
        if not args.quiet:
            print(f"  {'[%3d%%] ' % int(ratio * 100) if ratio >= 0 else '       '}{msg}", file=sys.stderr)

    ctx.progress = progress
    rep = pipe.run_agent("writer:rewrite", stage=Stage.DRAFT.value)
    print(f"{'✅' if rep.ok else '❌'} writer:rewrite：{rep.summary}")
    for d in rep.data.get("rewritten") or []:
        print(f"   · {d['section']}（{'整章' if d['mode'] == 'section' else '单段'}）"
              f"{d['chars_before']}→{d['chars_after']} 字")
    for f in rep.data.get("failed") or []:
        print(f"   ⏭ {str(f.get('target'))[:40]}：{f.get('reason')}")
    if not rep.ok:
        return 1

    # 重新定稿：改写把稿子拉回了证据池编号空间，必须再连续化并重建文献表。
    rep2 = pipe.run_agent("writer:references", stage=Stage.FINAL.value)
    print(f"{'✅' if rep2.ok else '❌'} writer:references：{rep2.summary}")

    out = ""
    if args.out:
        from .export.docx_writer import export_docx

        res = export_docx(
            board.draft_text, args.out,
            title=board.project.title,
            author=str(board.project.meta.get("author") or ""),
        )
        out = str(args.out)
        print(f"{'✅' if res.ok else '⚠'} 已导出：{out}（{res.paragraphs} 段 · "
              f"标题 {res.headings} 个 · 待办标记 {res.pending_marks} 处）")

    if args.json:
        _print({"rewrite": rep.as_dict(), "references": rep2.as_dict(), "export": out})
    led = ctx.ledger.snapshot()
    print(f"模型调用：{led['calls']} 次，失败 {led['failures']}，耗时 {led['seconds']}s")
    return 0


def cmd_models(args, settings: Settings) -> int:
    """模型档案：列出、增删、切换、按用途分配、测试连通性。"""
    from .models import PRESETS, ROLES, ModelProfile, ModelRegistry, preset_list, test_profile

    action = args.action or "list"
    reg = ModelRegistry.load()

    if action == "presets":
        if args.json:
            _print(preset_list())
            return 0
        for p in PRESETS:
            print(f"  {p.id:<12} {p.label}")
            print(f"      {p.base_url or '（自定义）'}   建议模型：{p.model or '（自填）'}")
            if p.note:
                print(f"      {p.note}")
        return 0

    if action == "list":
        if args.json:
            _print({**reg.public(), "routing": _routing_rows(reg)})
            return 0
        if not reg.profiles:
            print("还没有配置任何模型档案。")
            print("  · 看可选厂商：`models presets`")
            print("  · 加一个：    `models add --id deepseek --preset deepseek --key sk-xxx`")
            print("  （也可以继续用 .env 里的 LLM_* 单模型配置，本功能是可选的）")
            return 0
        print(f"模型档案（{len(reg.profiles)} 个，配置文件 {reg.path}）\n")
        for p in reg.profiles.values():
            mark = "★" if p.id == reg.active else " "
            state = "✅" if p.configured else "⚠ 未填全"
            print(f" {mark} {p.id:<14} {p.label or '':<16} {state}")
            print(f"      {p.base_url or '（缺 base_url）'}")
            print(f"      模型 {p.model or '（缺）'}   密钥 {p.masked_key() or '（缺）'}")
        print("\n用途分配（空 = 用上面带 ★ 的当前档案）：")
        for role, desc in ROLES:
            pid = reg.roles.get(role, "")
            prof = reg.profiles.get(pid) if pid else None
            target = f"{pid}（{prof.model}）" if prof else "当前档案"
            print(f"  {role:<10} → {target:<28} {desc}")
        warns = reg.missing_roles()
        if warns:
            print("\n⚠ 用途分配有问题：")
            for w in warns:
                print(f"  · {w}")
        for w in reg.problems:
            print(f"\n⚠ {w}")
        return 0

    if action == "add":
        pid = (args.id or "").strip()
        if not pid and args.preset:
            pid = args.preset
        if not pid:
            print("错误：请用 --id 指定档案名（例如 --id deepseek）", file=sys.stderr)
            return 2
        prof: ModelProfile | None = None
        if args.preset:
            try:
                prof = ModelProfile.from_preset(
                    args.preset, pid=pid, api_key=args.key or "", model=args.model or ""
                )
            except ValueError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
        else:
            prof = ModelProfile(id=pid)
        if args.label:
            prof.label = args.label
        if args.base_url:
            prof.base_url = args.base_url
        if args.model:
            prof.model = args.model
        if args.key is not None:
            prof.api_key = args.key
        if args.temperature is not None:
            prof.temperature = args.temperature
        if args.max_tokens:
            prof.max_tokens = args.max_tokens
        # 没给 key 时先沿用同名档案里已有的 key：改 base_url 不该逼作者重填一次密钥。
        old = reg.profiles.get(pid)
        if old is not None and not prof.api_key:
            prof.api_key = old.api_key
        # 再退一步：沿用 .env 里已配好的 key。**只在 base_url 完全相同时**——
        # 否则会把 A 家的密钥发到 B 家的服务器上，那是实打实的凭据泄露。
        inherited = ""
        if not prof.api_key and settings.llm.api_key.strip():
            if (prof.base_url or "").rstrip("/") == (settings.llm.base_url or "").rstrip("/"):
                prof.api_key = settings.llm.api_key
                inherited = settings.llm_key_source or ".env"
        reg.upsert(prof)
        if args.use:
            reg.active = pid
        reg.save()
        state = "✅ 已填全" if prof.configured else "⚠ 还没填全（缺 base_url / 模型名 / key）"
        print(f"已保存档案「{pid}」：{state}")
        print(f"  {prof.base_url or '（缺 base_url）'}   模型 {prof.model or '（缺）'}")
        if inherited:
            print(f"  密钥沿用了已配置的 {inherited}（base_url 相同，未复制到别处）")
        elif not prof.api_key:
            print("  还没有密钥。填法：`models add --id %s --key sk-xxx`" % pid)
        if not args.use:
            print(f"  要切换到这个档案：`models use {pid}`")
        return 0

    if action == "remove":
        pid = (args.id or "").strip()
        if not pid:
            print("错误：请用 --id 指定要删除的档案", file=sys.stderr)
            return 2
        if not reg.remove(pid):
            print(f"错误：没有这个档案：{pid}", file=sys.stderr)
            return 1
        reg.save()
        print(f"已删除档案「{pid}」（指向它的用途分配也已清除）")
        return 0

    if action == "use":
        pid = (args.id or "").strip()
        if pid not in reg.profiles:
            print(f"错误：没有这个档案：{pid}", file=sys.stderr)
            return 1
        reg.active = pid
        reg.save()
        prof = reg.profiles[pid]
        print(f"已切换到「{prof.label or pid}」：{prof.model or '（缺模型名）'} @ {prof.base_url or '（缺 base_url）'}")
        warns = reg.missing_roles()
        if warns:
            print("注意：下面这些用途单独指向了别的档案，不会跟着切换：")
            for w in warns:
                print(f"  · {w}")
        return 0

    if action == "role":
        pairs = list(args.set or [])
        if args.id and args.role:
            pairs.append(f"{args.role}={args.id}")
        if not pairs:
            print("用法：`models role --set draft=strong --set verify=cheap`（可重复）", file=sys.stderr)
            print("      `models role --role verify --id cheap` 也行。")
            print("      某用途的值留空 = 回到当前激活的档案。")
            print("      可用用途：" + "、".join(f"{r}({d})" for r, d in ROLES))
            return 2
        valid = {r for r, _ in ROLES}
        for pair in pairs:
            role, _, pid = pair.partition("=")
            role, pid = role.strip(), pid.strip()
            if role not in valid:
                print(f"错误：未知用途「{role}」，可用：{'、'.join(sorted(valid))}", file=sys.stderr)
                return 2
            if pid and pid not in reg.profiles:
                print(f"错误：没有这个档案：{pid}", file=sys.stderr)
                return 1
            reg.set_role(role, pid)
        reg.save()
        print("已更新用途分配：")
        for role, desc in ROLES:
            pid = reg.roles.get(role, "")
            print(f"  {role:<10} → {pid or '（当前档案）':<20} {desc}")
        return 0

    if action == "test":
        # 批量测试：配了多个厂商/多个档位时，逐个测一遍比一次测一个省事得多，
        # 也能一眼看出"哪几个能用、哪几个还差点什么"。
        if args.all:
            if not reg.profiles:
                print("错误：还没有配置任何档案", file=sys.stderr)
                return 2
            rows: list[dict] = []
            print(f"逐个测试 {len(reg.profiles)} 个档案（每个真发一次请求）…\n")
            for pid, prof in reg.profiles.items():
                if not prof.configured:
                    rows.append({"id": pid, "ok": False, "error": "还没填全（缺 base_url / 模型名 / key）"})
                    print(f"  ⏭ {pid:<16} 跳过：还没填全")
                    continue
                res = test_profile(prof, timeout=float(args.timeout or 30))
                rows.append({"id": pid, **res})
                mark = "✅" if res["ok"] else "❌"
                tail = f"回显：{res['reply']}" if res["ok"] else f"失败：{res['error'][:70]}"
                print(f"  {mark} {pid:<16} {res['model']:<22} {res['seconds']:>5.2f}s  {tail}")
            if args.json:
                _print(rows)
            ok_n = sum(1 for r in rows if r.get("ok"))
            print(f"\n可用 {ok_n}/{len(rows)} 个。")
            print("提示：`models role --set verify=<可用且便宜的档案>` 可以把它用在核验与修复上。")
            return 0 if ok_n else 1

        pid = (args.id or "").strip() or reg.active
        if not pid:
            print("错误：还没有配置任何档案", file=sys.stderr)
            return 2
        prof = reg.profiles.get(pid)
        if prof is None:
            print(f"错误：没有这个档案：{pid}", file=sys.stderr)
            return 1
        print(f"正在测试「{prof.label or pid}」：{prof.model} @ {prof.base_url} …")
        res = test_profile(prof, timeout=float(args.timeout or 30))
        if args.json:
            _print(res)
            return 0 if res["ok"] else 1
        if res["ok"]:
            print(f"  ✅ 连接成功（{res['seconds']}s），模型回显：{res['reply']}")
            return 0
        print(f"  ❌ 连接失败（{res['seconds']}s）：{res['error']}")
        print("     常见原因：密钥错、模型名不存在、余额不足、网络不通、base_url 少/多了 /v1。")
        return 1

    print(f"错误：未知操作 {action}", file=sys.stderr)
    return 2


def _routing_rows(reg) -> list[dict]:
    from .models import ROLES

    out = []
    for role, desc in ROLES:
        prof = reg.resolve(role)
        out.append({
            "role": role, "label": desc,
            "profile": prof.id if prof else "",
            "model": prof.model if prof else "",
            "explicit": bool(reg.roles.get(role)),
        })
    return out


def cmd_docx_format(args, settings: Settings) -> int:
    """把排版属性写回一份已有的 .docx（正文小四宋体、1.5 倍行距这类）。

    这是项目一直记在案的那个缺口：``app/format`` 原先只报偏差、不改属性。
    改属性与改文字是两件风险完全不同的事——文字改错读一遍就看得出来，
    **排版写坏可能到答辩前才发现**。所以这条命令把安全性做成机制：
    先备份、写完逐字比对文字、确认能重新打开，任一不过就自动还原。
    """
    from .format import FormatScheme, apply_scheme, inspect_docx, load_default_ruleset
    from .format import scheme_from_ruleset, scheme_from_template

    src = Path(args.input)
    if not src.exists():
        print(f"错误：文件不存在：{src}", file=sys.stderr)
        return 1

    # ---- 方案：模板 > 命令行 > 规则集默认 ----
    notes: list[str] = []
    if args.like:
        tpl = Path(args.like)
        if not tpl.exists():
            print(f"错误：模板文件不存在：{tpl}", file=sys.stderr)
            return 1
        scheme, tnotes = scheme_from_template(tpl)
        notes.extend(tnotes)
    else:
        scheme = scheme_from_ruleset(load_default_ruleset())

    def num(v, attr):
        if v is not None:
            setattr(scheme, attr, float(v))

    num(args.body_size, "body_size_pt")
    num(args.indent_chars, "body_first_line_chars")
    num(args.h1_size, "h1_size_pt")
    num(args.h2_size, "h2_size_pt")
    num(args.h3_size, "h3_size_pt")
    num(args.caption_size, "caption_size_pt")
    num(args.page_width, "page_width_cm")
    num(args.page_height, "page_height_cm")
    if args.body_font:
        scheme.body_cjk_font = args.body_font
    if args.latin_font:
        scheme.body_latin_font = args.latin_font
    if args.h1_font:
        scheme.h1_cjk_font = args.h1_font
    if args.h2_font:
        scheme.h2_cjk_font = args.h2_font
        scheme.h3_cjk_font = args.h2_font
    if args.align:
        if scheme.is_disabled("body_alignment"):
            # 对齐规则默认是关的（Word 默认左对齐，硬报会刷屏）。用户显式点名
            # 要改对齐时照改，但要讲清"这一项默认不在复验范围内"，否则就成了
            # 一次没人能核对的静默改动。
            notes.append(
                "正文对齐按你的指定写入；**对齐这一项默认不检查**"
                "（rules_default.yaml 里 body_alignment 是 enabled: false）——"
                "要让它同时被检查、被复验，把那条改成 true。"
            )
        scheme.body_align = "both" if args.align == "justify" else args.align
    # 没给 --align 就不动它：字段为 None（规则关着）时表示"不改对齐"，
    # 规则开着时它是规则集里的值。**不能在这里写 else 把它清成 None**，
    # 那会把"把 body_alignment 改成 true 就生效"这条承诺弄坏。
    if args.line_fixed is not None:
        scheme.body_line_mode, scheme.body_line_value = "fixed", float(args.line_fixed)
    elif args.line_spacing is not None:
        scheme.body_line_mode, scheme.body_line_value = "multiple", float(args.line_spacing)
    if args.margin:
        for pair in args.margin.split(","):
            k, _, v = pair.partition("=")
            attr = {
                "top": "margin_top_cm", "bottom": "margin_bottom_cm",
                "left": "margin_left_cm", "right": "margin_right_cm",
                "header": "header_cm", "footer": "footer_cm",
            }.get(k.strip())
            if not attr or not v.strip():
                print(f"错误：--margin 的写法应为 top=2.5,left=3.0,…（不认识 {pair!r}）", file=sys.stderr)
                return 2
            setattr(scheme, attr, float(v))

    info = inspect_docx(src)
    if args.json:
        pass
    else:
        print(f"输入：{src}（{info['paragraphs']} 段，{info['headings']} 个标题，"
              f"{info['tables']} 个表格，{info['chars']} 字）")
        print(f"方案：{scheme.describe()}")
        for n in notes:
            print(f"  · {n}")

    if args.check:
        # 只读：报告当前与方案的差距，不改文件。用现成的格式检查器。
        from .format import evaluate
        from .parsing.docx_reader import read_docx

        try:
            issues = evaluate(read_docx(src), load_default_ruleset())
        except Exception as exc:  # noqa: BLE001
            print(f"错误：读取格式事实失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print({"input": str(src), "scheme": scheme.to_dict(),
                    "issues": [i.as_dict() for i in issues]})
            return 0
        print(f"\n当前与规范的差距：{len(issues)} 条")
        try:
            from .format import WRITABLE_RULE_IDS

            writable = {f"format.{r}" for r in WRITABLE_RULE_IDS}
            n_w = sum(1 for i in issues if i.kind in writable)
            print(f"  其中 **可写回 {n_w} 条**（本命令能直接改掉），"
                  f"文本型 {len(issues) - n_w} 条（标点/空格/编号，归文本算子）")
        except Exception:  # noqa: BLE001 - 分类失败不影响主报告
            pass
        for i in issues[:40]:
            print(f"  [{i.severity.value:<7}] {i.kind}")
            print(f"      {i.message[:110]}")
        if issues:
            print("\n去掉 --check 即可把这些属性改过来（会先备份）。")
        return 0

    out = Path(args.out) if args.out else None
    rep = apply_scheme(src, out, scheme=scheme, backup=not args.no_backup)

    if args.json:
        _print(rep.as_dict())
        return 0 if rep.ok else 1

    print()
    if not rep.ok:
        print("❌ 写回失败，原文件未被改动：")
        for e in rep.errors:
            print(f"   {e}")
        for n in rep.notes:
            print(f"   {n}")
        return 1

    print(f"✅ 已写出：{rep.output}")
    if rep.backup:
        print(f"   原文件备份：{rep.backup}")
    print(f"   正文 {rep.paragraphs_body} 段 · 标题 {rep.paragraphs_heading} 个 · "
          f"题注 {rep.paragraphs_caption} 个 · 处理 {rep.runs_styled} 处文字 · "
          f"样式 {len(rep.styles_updated)} 个 · 页面 {rep.sections_updated} 处")
    print("\n验证：")
    print(f"   ① 文字一字未改：{'✅ 通过' if rep.text_unchanged else '❌ 不通过（已还原）'}")
    print(f"   ② 改完能重新打开：{'✅ 通过' if rep.reopened_ok else '❌ 不通过'}")
    print(f"   ③ 段落数：{rep.paragraphs_before} → {rep.paragraphs_after}")
    for n in rep.notes:
        print(f"   · {n}")

    # 用同一套格式检查器复验：改前报的偏差改后应当消失。
    #
    # **"改前"必须从备份读，不能从 src 读**：原地修改时 src 已经是改过的文件了，
    # 两边读同一个文件会得到"113 条 → 113 条"这种看起来"完全没生效"的假结论。
    # 这个坑踩过。
    #
    # 复验要**分两类报**：本功能只改属性，"参考文献编号/半角标点/中英文之间空格"
    # 这类文本型偏差它一条也改不了。混在一起报总数，作者会把"还剩 69 条"读成
    # "这功能没用"——而实际上能改的那 44 条已全部归零。
    try:
        from .format import WRITABLE_RULE_IDS, evaluate
        from .parsing.docx_reader import read_docx

        rs = load_default_ruleset()
        writable = {f"format.{r}" for r in WRITABLE_RULE_IDS}
        # "改前"从哪儿读：
        #   · 原地覆盖 → 只能从 .bak 读（src 已经改过了）
        #   · 输出到新文件 → src 根本没动，直接从 src 读（这时没有 .bak）
        # 早先只认备份，于是 -o 到新文件时复验被跳过，"改前多少条"就报不出来。
        if rep.backup and Path(rep.backup).exists():
            before_src: Path | None = Path(rep.backup)
        elif not rep.in_place and Path(rep.source).exists():
            before_src = Path(rep.source)
        else:
            before_src = None
        if before_src is not None:
            b_issues = evaluate(read_docx(before_src), rs)
            a_issues = evaluate(read_docx(Path(rep.output)), rs)
            b_w = [i for i in b_issues if i.kind in writable]
            a_w = [i for i in a_issues if i.kind in writable]
            b_t = len(b_issues) - len(b_w)
            a_t = len(a_issues) - len(a_w)
            print(f"   ④ 格式检查器复验：{len(b_issues)} 条偏差 → {len(a_issues)} 条")
            print(f"      · 可写回类（字体/字号/行距/缩进/对齐/页面）："
                  f"{len(b_w)} 条 → {len(a_w)} 条")
            print(f"      · 文本类（标点/空格/编号/题注缺失等，归文本算子，本功能不动）："
                  f"{b_t} 条 → {a_t} 条")
            if a_w:
                kinds = sorted({i.kind for i in a_w})
                print(f"      ⚠ 仍有可写回类偏差没改掉：{'、'.join(kinds[:6])}"
                      f"{' 等' if len(kinds) > 6 else ''}——请把这份文件发我看看")
            else:
                print("      ✅ 可写回类偏差已全部归零（同一套检查器量的，可自行复核）")
            if len(a_issues) >= len(b_issues):
                print("      ⚠ 总偏差没有减少，请把这份文件发我看看（可能是样式继承链特殊）")
        else:
            print("   ④ 未做复验：这次既没有备份、源文件也不是可比对的原始版本。"
                  "建议留备份，它可以让你随时退回改之前的状态。")
    except Exception as exc:  # noqa: BLE001 - 复验失败不影响交付，但要说一声
        print(f"   ④ 复验未执行：{type(exc).__name__}: {exc}")
    return 0


def cmd_review(args, settings: Settings) -> int:
    """生成模拟审稿意见书：把问题清单按审稿人的读法组织成五个维度。"""
    from .review import build_review, render_review

    board = Blackboard.load(PROJECTS_DIR, args.project)
    issues = list(board.project.issues.values())
    if args.open_only:
        issues = [i for i in issues if i.status == "open"]
    report = build_review(issues, title=board.project.title)
    text = render_review(report)

    if args.json:
        _print(report.as_dict())
    else:
        print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\n已写出 {out}", file=sys.stderr)
    # 有致命问题就以非零码退出，便于脚本化判断"能不能进下一步"。
    return 0 if report.counts.get("CRITICAL", 0) == 0 else 1


def cmd_run_agent(args, settings: Settings) -> int:
    """只跑一个已注册的智能体。

    为什么需要它：写作链路是"规划→起草→结论→文献表"，其中**文献表这一步是纯本地的、
    零成本的**。改了文献表的生成规则（比如补齐字段、改格式）之后，重跑整条起草链路
    要几十次模型调用，而重跑这一步一次都不用——所以必须能单独跑它。

    只对**本地智能体**（``uses_model=False``）默认放行；要跑会用模型的智能体得显式加
    ``--allow-model``，免得一条命令意外烧掉几十次调用。
    """
    from .framework.artifact import Stage
    from .pipeline import Runner
    from .runtime import build_runtime

    rt = build_runtime(settings)
    try:
        agent = rt.registry.get(args.agent)
    except KeyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if agent.uses_model and not args.allow_model:
        print(
            f"错误：`{args.agent}` 会调用模型，可能产生费用。确认要跑请加 --allow-model。",
            file=sys.stderr,
        )
        return 2

    runner = Runner(runtime=rt)
    board = runner.load(args.project)
    ctx = rt.new_context(board, stage=Stage.DRAFT.value)
    from .framework.state import Pipeline as _Pipeline

    pipe = _Pipeline(rt.registry, ctx)
    report = pipe.run_agent(args.agent, stage=args.stage or agent.stage)

    if args.json:
        _print(report.as_dict())
        return 0 if report.ok else 1
    mark = "✅" if report.ok else "❌"
    print(f"{mark} {report.agent}：{report.summary}")
    if report.error:
        print(f"   错误：{report.error}")
    if report.issues:
        print(f"   新增问题：{len(report.issues)} 条")
    led = ctx.ledger.snapshot()
    if led["calls"]:
        print(f"   模型调用：{led['calls']} 次，耗时 {led['seconds']}s")
        for name, v in sorted((led.get("by_model") or {}).items(), key=lambda kv: -kv[1]["calls"]):
            print(f"     · {name}：{v['calls']} 次")
    return 0 if report.ok else 1


def cmd_export(args, settings: Settings) -> int:
    from .config import RUNS_DIR

    board = Blackboard.load(PROJECTS_DIR, args.project)

    # 按输出后缀决定格式：给了 .docx 就导出规范排版的 Word，
    # 否则写纯文本。这样"导出成 Word"不需要再记一个 flag。
    want_docx = bool(args.out) and Path(args.out).suffix.lower() == ".docx"
    if want_docx and args.kind != "draft":
        print("错误：只有 --kind draft 能导出为 .docx", file=sys.stderr)
        return 2
    default_suffix = ".docx" if want_docx else ".txt"
    out = Path(args.out) if args.out else (RUNS_DIR / f"{board.project.id}_{args.kind}{default_suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.kind == "draft":
        if want_docx:
            from .export.docx_writer import export_docx

            res = export_docx(
                board.draft_text,
                out,
                title=board.project.title,
                author=str(board.project.meta.get("author") or ""),
            )
            if not res.ok:
                print(f"错误：导出失败 {res.verification.get('error') or ''}", file=sys.stderr)
                return 1
            print(f"已导出到 {out}（{out.stat().st_size} 字节）")
            print(f"  {res.paragraphs} 段 · 标题 {res.headings} 个 · 文献 {res.references} 条 · "
                  f"待办标记 {res.pending_marks} 处")
            for n in res.notes:
                print(f"  - {n}")
            return 0
        out.write_text(board.draft_text, encoding="utf-8")
    elif args.kind == "issues":
        lines = []
        for i in board.open_issues():
            lines.append(f"[{i.severity.value}] {i.kind}: {i.message}")
            if i.evidence:
                lines.append(f"    证据：{i.evidence[:200]}")
            if i.fix_hint:
                lines.append(f"    建议：{i.fix_hint[:300]}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif args.kind == "version":
        target = None
        for v in board.versions("draft"):
            if v.version == args.version:
                target = v
                break
        if target is None:
            print(f"错误：没有 v{args.version}。现有版本：{[v.version for v in board.versions('draft')]}", file=sys.stderr)
            return 1
        out.write_text(target.text, encoding="utf-8")
    else:
        print(f"错误：未知导出类型 {args.kind}", file=sys.stderr)
        return 1

    print(f"已导出到 {out}（{out.stat().st_size} 字节）")
    return 0


def cmd_versions(args, settings: Settings) -> int:
    board = Blackboard.load(PROJECTS_DIR, args.project)
    for v in board.versions("draft"):
        note = "；".join(v.notes) or "—"
        print(f"  v{v.version:<3} {v.id}  {len(v.text):>7} 字  by {v.created_by:<14} {note[:60]}")
    return 0


def cmd_peers(args, settings: Settings) -> int:
    from .adapters.rag import RAGAdapter

    rag = RAGAdapter(settings.peers.rag_base, timeout=20)
    docs = rag.docs()
    if args.json:
        _print(docs)
        return 0
    print(f"知识库共 {len(docs)} 篇：\n")
    for d in docs:
        print(f"  {d.get('doc_id','')}  {d.get('year') or '----'}  "
              f"{(d.get('author') or '佚名'):<8} {d.get('n_chunks',0):>4} 块  {d.get('title','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="论文多智能体系统 · 命令行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  agents                     看有哪些能力可用\n"
            "  health                     检查两个对端服务是否在跑\n"
            "  propose 开题报告.docx       先看系统把开题读成了什么（不花钱）\n"
            "  thesis --proposal 开题.docx 一键生成论文初稿（.docx + 生成报告）\n"
            "  new --file 稿子.docx       建立项目（已有稿子要改时用）\n"
            "  run -p <项目id>            跑完整流水线\n"
            "  run -p <项目id> -s verify  只跑验证\n"
            "  issues -p <项目id>         看待处理问题\n"
            "  export -p <项目id>         导出稿件\n"
        ),
    )
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（全局）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("thesis", help="一键生成：开题报告 / 任意题目 → 论文初稿")
    p.add_argument("--proposal", help="开题报告文件（.docx/.txt/.md）；也可以不给，直接用 --topic")
    p.add_argument("--topic", default="", help="论文题目（没有开题报告时用它）")
    p.add_argument("--title", default="", help="论文题目（覆盖开题里的题目）")
    p.add_argument("--author", default="", help="作者姓名（写入导出文档）")
    p.add_argument("--discipline", default="", help="学科")
    p.add_argument("--genre", default="",
                   help="论文类型：empirical 实证研究型 / case 案例研究型 / theoretical 理论思辨型 / "
                        "engineering 设计开发型 / experimental 实验研究型 / survey 调查研究型 / review 文献综述型。"
                        "不给则按题目自动判定")
    p.add_argument("--requirement", default="",
                   help="写作要求（自由文本），会逐条进到规划与起草的提示词")
    p.add_argument("--degree", default="硕士", help="学位层次（只影响默认字数）")
    p.add_argument("--model", default="",
                   help="本次使用哪个模型档案（见 `models list`）；不给则用按用途分配的模型")
    p.add_argument("--words", type=int, default=0, help="目标字数；不给则用该论文类型的常见值")
    p.add_argument("--section", action="append",
                   help="手工指定章节，可重复；给了就覆盖开题里的章节树")
    p.add_argument("--chapter", action="append",
                   help="**只写**指定章节（可重复，按标题片段匹配）")
    p.add_argument("--skip-chapter", action="append",
                   help="**跳过**指定章节（可重复；这几章保留稿件里已有的内容）")
    p.add_argument("--out", help="输出 .docx 路径")
    p.add_argument("--report", help="生成报告 .md 路径")
    p.add_argument("-s", "--stages", action="append",
                   help="只跑指定阶段，可重复：retrieve/draft/verify/fix/final")
    p.add_argument("--no-export", action="store_true", help="不导出 .docx")
    p.add_argument("--no-precheck", action="store_true",
                   help="跳过生成前的知识库相关性预检（默认会预检，零成本）")
    p.add_argument("--force", action="store_true",
                   help="预检判定知识库不相关时仍然继续（不推荐：相关章节会写不出来）")
    p.add_argument("--llm-verify", action="store_true",
                   help="启用需要调用模型的检查器（更全面，也更花钱）")
    p.add_argument("--with-dedup", action="store_true", help="附加降 AIGC 阶段（需 aigc-reducer 在线）")
    p.add_argument("--with-format", action="store_true", help="附加格式检查阶段")
    p.add_argument("--with-gather", action="store_true", help="附加资料扫描入库阶段（有副作用）")
    p.add_argument("--max-rounds", type=int, default=2, help="自我更正最大轮数")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_thesis)

    p = sub.add_parser("models", help="模型档案：多厂商 API 接入、切换、按用途分配、连通性测试")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "presets", "add", "remove", "use", "role", "test"])
    p.add_argument("--id", default="", help="档案名（add/remove/use/test 用）")
    p.add_argument("--preset", default="", help="add：厂商预设 id，见 `models presets`")
    p.add_argument("--label", default="", help="add：显示名")
    p.add_argument("--base-url", default="", help="add：API 地址（OpenAI 兼容，通常以 /v1 结尾）")
    p.add_argument("--key", default=None, help="add：API Key（本地模型可随便填占位值）")
    p.add_argument("--model", default="", help="add：模型名")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=0)
    p.add_argument("--use", action="store_true", help="add 之后顺手切换到这个档案")
    p.add_argument("--role", default="", help="role：用途名（draft/plan/conclude/rewrite/verify/fix/polish/other）")
    p.add_argument("--set", action="append", help="role：批量设置，形如 draft=deepseek-cheap，可重复")
    p.add_argument("--timeout", type=float, default=30.0, help="test：超时秒数")
    p.add_argument("--all", action="store_true", help="test：逐个测试全部档案")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("kb", help="知识库：查库 / 试查 / 预检 / 投料入库")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "list", "search", "check", "add", "ingest"],
                   help="status 概况｜list 文献清单｜search 试查｜check 按题目预检｜add 投料｜ingest 触发入库")
    p.add_argument("query", nargs="?", help="search 的查询词")
    p.add_argument("--topic", help="check 用的论文题目")
    p.add_argument("--requirement", default="", help="check 时一并考虑的写作要求")
    p.add_argument("files", nargs="*", help="add：要投料的文件")
    p.add_argument("--dir", action="append", help="add：递归投料一个目录，可重复")
    p.add_argument("-k", type=int, default=8, help="检索条数")
    p.add_argument("--no-rerank", action="store_true", help="search 时不做重排（更快、不花钱）")
    p.add_argument("--no-ingest", action="store_true", help="add 时只复制文件、不触发入库")
    p.add_argument("--force", action="store_true", help="add/ingest 时强制重入已存在的文件")
    p.set_defaults(func=cmd_kb)

    p = sub.add_parser("rewrite", help="按你的要求改写指定章节或原文片段")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("--section", default="", help="要改写的章节（按标题片段匹配，需唯一）")
    p.add_argument("--find", default="", help="要改写的原文片段（从稿件里逐字复制）")
    p.add_argument("--instruction", default="", help="改写要求（例如「把机制分析补实」）")
    p.add_argument("--targets", default="", help='批量目标（JSON）：[{"section":"第3章","instruction":"…"}]')
    p.add_argument("--model", default="", help="本次使用哪个模型档案（见 `models list`）")
    p.add_argument("-o", "--out", help="改完后重新导出为 .docx")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_rewrite)

    p = sub.add_parser("propose", help="只解析开题报告，看系统读到了什么（不花钱）")
    p.add_argument("proposal", help="开题报告文件（.docx/.txt/.md）")
    p.add_argument("--title", default="", help="题目兜底（文档里没有题目行时使用）")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("docx-format", help="把排版写回已有 .docx（正文小四宋体、1.5 倍行距…）")
    p.add_argument("input", help="要整理的 .docx")
    p.add_argument("-o", "--out", help="输出路径；不给则**覆盖原文件**（自动留 .bak 备份）")
    p.add_argument("--like", help="照这份 .docx 模板的格式改（读取它的样式与页面设置）")
    p.add_argument("--check", action="store_true", help="只报告当前与规范的差距，不改文件")
    p.add_argument("--body-font", help="正文中文字体（默认 宋体）")
    p.add_argument("--latin-font", help="正文西文字体（默认 Times New Roman）")
    p.add_argument("--body-size", type=float, help="正文字号磅值（小四=12）")
    p.add_argument("--line-spacing", type=float, help="倍数行距（1.5 = 1.5 倍）")
    p.add_argument("--line-fixed", type=float, help="固定值行距磅值（如 22）")
    p.add_argument("--indent-chars", type=float, help="首行缩进字符数（默认 2）")
    p.add_argument("--align", choices=["both", "left", "center", "justify"],
                   help="正文对齐方式（默认 both=两端对齐；justify 同 both）")
    p.add_argument("--h1-font", help="一级标题中文字体（默认 黑体）")
    p.add_argument("--h2-font", help="二/三级标题中文字体（默认 黑体）")
    p.add_argument("--h1-size", type=float, help="一级标题字号（三号=16）")
    p.add_argument("--h2-size", type=float, help="二级标题字号（四号=14）")
    p.add_argument("--h3-size", type=float, help="三级标题字号（小四=12）")
    p.add_argument("--caption-size", type=float, help="图表题注字号（五号=10.5）")
    p.add_argument("--page-width", type=float, help="页面宽（厘米，A4=21）")
    p.add_argument("--page-height", type=float, help="页面高（厘米，A4=29.7）")
    p.add_argument("--margin", help="页边距，形如 top=2.5,left=3.0,bottom=2.5,right=2.5")
    p.add_argument("--no-backup", action="store_true", help="不写 .bak（不推荐）")
    p.set_defaults(func=cmd_docx_format)

    p = sub.add_parser("review", help="生成模拟审稿意见书（把问题清单整理成五个维度）")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("--open-only", action="store_true", help="只看未处理的问题")
    p.add_argument("-o", "--out", help="把意见书写到文件（.md）")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("run-agent", help="只跑一个智能体（如重跑 writer:references，零模型成本）")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("agent", help="智能体名，见 `agents` 命令，例如 writer:references")
    p.add_argument("--stage", default="", help="记到哪个阶段名下（默认用智能体自己的阶段）")
    p.add_argument("--allow-model", action="store_true", help="允许跑会调用模型的智能体（会产生费用）")
    p.set_defaults(func=cmd_run_agent)

    p = sub.add_parser("agents", help="列出智能体、检查器与修复算子")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("health", help="检查对端服务可达性")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("new", help="新建项目")
    p.add_argument("--file", help="稿件文件（.docx/.txt/.md）")
    p.add_argument("--text", help="稿件文本文件路径（与 --file 二选一）")
    p.add_argument("--title", default="", help="项目标题")
    p.add_argument("--topic", default="", help="研究主题（用于写作规划）")
    p.add_argument("--query", action="append", help="检索查询，可重复")
    p.add_argument("--root", action="append", help="额外资料目录，可重复")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("projects", help="列出项目")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("status", help="查看项目状态")
    p.add_argument("-p", "--project", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("issues", help="查看问题清单")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("--open-only", action="store_true", help="只看未处理")
    p.add_argument("--status", help="按状态过滤：open/fixed/needs_author")
    p.add_argument("--fixable", action="store_true", help="只看可机械修复的")
    p.set_defaults(func=cmd_issues)

    p = sub.add_parser("run", help="运行流水线")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("-s", "--stages", action="append",
                   help="只跑指定阶段，可重复：gather/retrieve/draft/verify/fix/dedup/format/final")
    p.add_argument("--max-rounds", type=int, help="自我更正最大轮数")
    p.add_argument("--no-llm-verify", action="store_true", help="跳过需要模型的检查器（省钱）")
    p.add_argument("--dry-run", action="store_true", help="不修改稿件")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("search", help="检索知识库（不花钱，除非开重排）")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--no-rerank", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("ask", help="基于知识库问答")
    p.add_argument("question")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--quiet", action="store_true", help="不打印来源列表")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("scan", help="扫描本地资料目录，看哪些能入库")
    p.add_argument("--root", action="append", help="额外目录，可重复")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("peers", help="列出知识库里的论文")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("versions", help="列出稿件版本链")
    p.add_argument("-p", "--project", required=True)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("export", help="导出稿件 / 问题清单 / 指定版本（-o 给 .docx 时导出规范排版的 Word）")
    p.add_argument("-p", "--project", required=True)
    p.add_argument("--kind", choices=["draft", "issues", "version"], default="draft")
    p.add_argument("--version", type=int, default=0, help="kind=version 时指定版本号")
    p.add_argument("-o", "--out", help="输出路径；后缀为 .docx 时导出 Word（仅 kind=draft）")
    p.set_defaults(func=cmd_export)

    return ap


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    ap = build_parser()
    args = ap.parse_args(argv)
    settings = load_settings()
    try:
        return args.func(args, settings)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
