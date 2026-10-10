"""黑板（Blackboard）。

多智能体系统里最容易被做坏的一层。常见的两个坑：

1. **共享可变状态**：所有 agent 直接改同一份文档，出问题时无法回答"谁改的"。
2. **只存最终结果**：中间版本被覆盖，自我更正循环失去对照物。

这里的做法是：**工件不可变**。每次修改产生一个新的 ``Artifact`` 版本，用
``parent`` 串成版本链。稿件只有一个，所以并发写不会发生；其余都是只读引用。

另外提供一个关键能力：``refresh_issue_status``。稿件改动后，旧问题清单里的
每条问题都要重新判定——这是自我更正循环里"复验"的核心，而不是简单地相信
修复器说"我改好了"。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifact import Artifact, Issue, Patch, Severity, Stage, new_id, severity_from


def _read_json(path: Path) -> Any:
    """读一个 JSON 文件，**容忍 UTF-8 BOM**。

    为什么要专门处理 BOM：它不产生任何可见字符，却会让 ``json.loads``
    在第一个字节就失败。而 BOM 是 Windows 上极常见的产物——记事本、
    PowerShell 的 ``Set-Content -Encoding UTF8``、不少编辑器都会加。
    实测踩过：手工修一个项目的 ``project.json`` 之后，整个项目打不开了，
    报的还是 "line 1 column 1" 这种看不出原因的错。

    **项目文件打不开对作者来说是很重的故障**：他看不到自己的稿子、问题清单与
    版本链。为了一个不可见的前缀把整份项目拒之门外，代价与收益完全不成比例。
    所以这里用 ``utf-8-sig``：有 BOM 就吃掉它，没有也照常读。
    """
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass
class Project:
    """一个论文项目。所有状态都在这一个对象里，可整体序列化。"""

    id: str
    title: str = "未命名项目"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 稿件文本的权威副本。每次修改都进 artifacts，这里只放"当前版本的 id"。
    stage: str = Stage.INTAKE.value
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    """artifact_id -> Artifact。历史版本全部保留。"""

    heads: dict[str, str] = field(default_factory=dict)
    """kind -> 当前版本的 artifact_id。'draft' 就是当前稿子。"""

    issues: dict[str, Issue] = field(default_factory=dict)
    patches: dict[str, Patch] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    """时间线：(ts, stage, round, actor, action, detail)"""

    meta: dict[str, Any] = field(default_factory=dict)
    """自由字段：学校模板路径、格式规范、作者信息、字数统计等。"""

    def as_dict(self, *, with_text: bool = True) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stage": self.stage,
            "heads": dict(self.heads),
            "artifacts": {
                aid: a.as_dict(with_text=with_text) for aid, a in self.artifacts.items()
            },
            "issues": {iid: i.as_dict() for iid, i in self.issues.items()},
            "patches": {pid: p.as_dict() for pid, p in self.patches.items()},
            "history": self.history[-400:],
            "meta": self.meta,
        }


class Blackboard:
    """项目的读写入口。所有智能体只通过它接触状态。"""

    def __init__(self, root: Path, project: Project) -> None:
        self.root = Path(root)
        self.project = project
        self.dir = self.root / project.id
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- 生命周期 ---------------------------------------------------------
    @classmethod
    def create(cls, root: Path, *, title: str, text: str = "", meta: dict | None = None) -> "Blackboard":
        pid = f"prj-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        project = Project(id=pid, title=title or "未命名项目", meta=dict(meta or {}))
        board = cls(root, project)
        if text:
            board.put_artifact(
                kind="draft",
                text=text,
                created_by="intake",
                stage=Stage.INTAKE.value,
                note="初始稿件",
            )
        board.record(Stage.INTAKE.value, 0, "system", "create", f"建立项目 {pid}")
        board.save()
        return board

    @classmethod
    def load(cls, root: Path, project_id: str) -> "Blackboard":
        path = Path(root) / project_id / "project.json"
        if not path.exists():
            raise FileNotFoundError(f"项目不存在: {project_id}")
        raw = _read_json(path)
        project = Project(
            id=raw["id"],
            title=raw.get("title", ""),
            created_at=raw.get("created_at", time.time()),
            updated_at=raw.get("updated_at", time.time()),
            stage=raw.get("stage", Stage.INTAKE.value),
            heads=raw.get("heads", {}),
            meta=raw.get("meta", {}),
            history=raw.get("history", []),
        )
        for aid, a in (raw.get("artifacts") or {}).items():
            project.artifacts[aid] = Artifact(
                id=a["id"],
                kind=a["kind"],
                text=a.get("text", ""),
                payload=a.get("payload", {}),
                version=a.get("version", 1),
                created_by=a.get("created_by", ""),
                created_at_stage=a.get("created_at_stage", ""),
                parent=a.get("parent", ""),
                run_id=a.get("run_id", ""),
                notes=list(a.get("notes") or []),
            )
        for iid, i in (raw.get("issues") or {}).items():
            project.issues[iid] = Issue(
                id=i["id"],
                kind=i.get("kind", ""),
                severity=severity_from(i.get("severity")),
                message=i.get("message", ""),
                location=i.get("location", {}),
                evidence=i.get("evidence", ""),
                detected_by=i.get("detected_by", "rules"),
                confidence=float(i.get("confidence", 1.0)),
                fixable=bool(i.get("fixable", False)),
                fix_op=i.get("fix_op", ""),
                fix_hint=i.get("fix_hint", ""),
                status=i.get("status", "open"),
                resolved_by=i.get("resolved_by", ""),
                resolved_at_stage=i.get("resolved_at_stage", ""),
                resolution_note=i.get("resolution_note", ""),
            )
        for pid, p in (raw.get("patches") or {}).items():
            project.patches[pid] = Patch(
                id=p["id"],
                issue_ids=list(p.get("issue_ids") or []),
                ops=list(p.get("ops") or []),
                rationale=p.get("rationale", ""),
                created_by=p.get("created_by", ""),
                applied=bool(p.get("applied", False)),
                error=p.get("error", ""),
                failed_op_index=int(p.get("failed_op_index", -1)),
            )
        return cls(root, project)

    @staticmethod
    def list_projects(root: Path) -> list[dict]:
        root = Path(root)
        if not root.exists():
            return []
        out: list[dict] = []
        for d in sorted(root.iterdir()):
            f = d / "project.json"
            if not f.is_file():
                continue
            try:
                raw = _read_json(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            draft = None
            head = (raw.get("heads") or {}).get("draft")
            if head:
                draft = (raw.get("artifacts") or {}).get(head)
            issues = list((raw.get("issues") or {}).values())
            out.append(
                {
                    "id": raw.get("id", d.name),
                    "title": raw.get("title", ""),
                    "stage": raw.get("stage", ""),
                    "updated_at": raw.get("updated_at", 0),
                    "chars": len((draft or {}).get("text", "")),
                    "artifacts": len(raw.get("artifacts") or {}),
                    "issues_open": sum(1 for i in issues if i.get("status") == "open"),
                    "issues_total": len(issues),
                }
            )
        return sorted(out, key=lambda x: x["updated_at"], reverse=True)

    def save(self) -> None:
        """原子写入 project.json，**对 Windows 的瞬时占用做重试**。

        为什么要重试：这里用的是"写临时文件再 replace"这个标准原子写法，而
        ``os.replace`` 在 Windows 上会在目标文件被其它句柄占用时抛
        ``PermissionError [WinError 5]``。占用者通常不是本程序——杀毒软件的实时扫描、
        同步盘、资源管理器预览都会短暂持有文件句柄。

        实测代价很大：一次生成结束时恰好撞上，**整条流水线崩在保存状态这一步**，
        前面几十次模型调用全部作废。而这类占用是毫秒级的，重试几次就能过去。

        重试仍失败就抛出，并且把原因说清楚——绝不能静默丢状态：
        状态没落盘意味着"作者看到的进度"和"磁盘上的进度"不一致，那比报错糟得多。
        """
        self.project.updated_at = time.time()
        path = self.dir / "project.json"
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(self.project.as_dict(with_text=True), ensure_ascii=False, indent=1)

        last: Exception | None = None
        for attempt in range(6):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.12 * (attempt + 1))
            except OSError as exc:  # noqa: PERF203 - 同样的瞬时占用也可能报 OSError
                last = exc
                time.sleep(0.12 * (attempt + 1))
        raise RuntimeError(
            f"保存项目状态失败（{path}）：{type(last).__name__}: {last}。"
            "通常是有其它程序正在占用该文件（杀毒扫描、同步盘、资源管理器预览）。"
            "请重试；若持续失败，请先把该项目目录排除出杀毒/同步范围。"
        ) from last

    def record(self, stage: str, round_no: int, actor: str, action: str, detail: str = "") -> None:
        self.project.history.append(
            {
                "ts": time.time(),
                "stage": stage,
                "round": round_no,
                "actor": actor,
                "action": action,
                "detail": detail,
            }
        )

    # -- 工件 -------------------------------------------------------------
    @property
    def draft(self) -> Artifact | None:
        head = self.project.heads.get("draft")
        return self.project.artifacts.get(head) if head else None

    @property
    def draft_text(self) -> str:
        d = self.draft
        return d.text if d else ""

    def head(self, kind: str) -> Artifact | None:
        hid = self.project.heads.get(kind)
        return self.project.artifacts.get(hid) if hid else None

    def put_artifact(
        self,
        *,
        kind: str,
        text: str = "",
        payload: dict | None = None,
        created_by: str,
        stage: str = "",
        run_id: str = "",
        note: str = "",
    ) -> Artifact:
        """写一个新版本。

        稿件类工件（draft）自动继承上一版文本与父子关系，形成版本链；
        从属工件（references / reports）每次都是新的头部。
        """
        prev = self.head(kind)
        if prev is not None and kind == "draft" and not text:
            text = prev.text

        art = Artifact(
            id=new_id("art"),
            kind=kind,
            text=text,
            payload=dict(payload or {}),
            version=(prev.version + 1) if prev else 1,
            created_by=created_by,
            created_at_stage=stage,
            parent=prev.id if prev else "",
            run_id=run_id,
            notes=[note] if note else [],
        )
        self.project.artifacts[art.id] = art
        self.project.heads[kind] = art.id
        return art

    def versions(self, kind: str) -> list[Artifact]:
        return sorted(
            (a for a in self.project.artifacts.values() if a.kind == kind),
            key=lambda a: a.version,
        )

    # -- 问题 -------------------------------------------------------------
    def add_issues(self, issues: Iterable[Issue], *, stage: str = "", round_no: int = 0) -> int:
        """Merge issues, de-duplicating by identity.

        Identity is ``(kind, location, evidence)`` rather than a fresh id, so the
        same problem found in round 2 does not appear as a new issue — it updates
        the existing one. Without this the loop looks like it never converges.
        """
        added = 0
        for issue in issues:
            key = self._identity(issue)
            existing = self.project.issues.get(key)
            if existing is None:
                issue.id = key
                self.project.issues[key] = issue
                added += 1
            else:
                # A re-detection means the problem survived the previous fix.
                # Exception: an issue already handed to the author stays in that
                # state, otherwise every round would re-flag the same "needs your
                # judgement" items as if they were new work.
                if existing.status != "needs_author":
                    existing.status = "open"
                    existing.resolved_by = ""
                    existing.resolution_note = ""
                existing.severity = issue.severity
                existing.message = issue.message
                existing.confidence = issue.confidence
                existing.fixable = issue.fixable
                existing.fix_op = issue.fix_op
                existing.fix_hint = issue.fix_hint
        return added

    @staticmethod
    def _identity(issue: Issue) -> str:
        """Stable identity for de-duplication and for round-to-round tracking.

        **位置类字段必须排除在外。** 这一点是被真实运行教会的：早期版本用
        ``json.dumps(location, sort_keys=True)`` 整体参与哈希，于是

        * 同一个问题出现在两处（``[7]`` 被引用两次而列表里没有）会被报成 2 条；
        * 更糟的是 ``char`` / ``paragraph`` 这类偏移**在文本被编辑后就会变化**，
          于是"第 2 轮仍然存在的老问题"会拿到一个**新 id**，看起来像新问题——
          循环的收敛判据因此失真。

        现在只保留语义性与可复验的定位：``find``（唯一锚点，也是修复的落点）、
        ``ref``（参考文献编号等稳定标识）、``stage``、``queries``。
        标题类字段（char/paragraph）即便不同，也认定为同一条问题并合并。
        """
        loc = issue.location or {}
        anchor = str(loc.get("find") or "").strip()
        if not anchor:
            # 没有唯一锚点时退回语义性标识。ref 是稳定的（编号不会被编辑改变），
            # 所以同一条参考文献的同一个问题不会因重新排版而变成新问题。
            anchor = "|".join(
                str(loc[k]) for k in ("ref", "stage", "query") if loc.get(k) not in (None, "")
            )
        if not anchor:
            # 最后退到区间类字段：它们不稳定，但总比把两条无关问题合并要好。
            anchor = "|".join(
                str(loc[k]) for k in ("paragraph", "char") if loc.get(k) is not None
            )
        ev = re.sub(r"\s+", " ", (issue.evidence or "")).strip()[:60]
        basis = f"{issue.kind}|{anchor}|{ev}"
        return "iss-" + uuid.uuid5(uuid.NAMESPACE_URL, basis).hex[:12]

    def open_issues(self, *, min_severity: str | None = None) -> list[Issue]:
        sev = severity_from(min_severity).rank if min_severity else -1
        out = [i for i in self.project.issues.values() if i.status == "open" and i.rank >= sev]
        return sorted(out, key=lambda i: (-i.rank, i.kind, i.id))

    def issues_by_kind(self) -> dict[str, list[Issue]]:
        out: dict[str, list[Issue]] = {}
        for i in self.project.issues.values():
            out.setdefault(i.kind, []).append(i)
        return out

    def severity_counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "major": 0, "minor": 0, "info": 0, "fixed": 0, "needs_author": 0}
        for i in self.project.issues.values():
            if i.status == "fixed":
                counts["fixed"] += 1
            elif i.status == "needs_author":
                counts["needs_author"] += 1
            elif i.status == "open":
                counts[i.severity.value] = counts.get(i.severity.value, 0) + 1
        return counts

    def refresh_issue_status(self, text: str) -> dict[str, int]:
        """Re-judge existing issues against the current text.

        This is what makes the self-correction loop honest. A fixer claiming
        success is not evidence; the issue is closed only when its own evidence
        can no longer be located in the document.

        Returns a small census so the caller can log what changed.
        """
        census = {"still_present": 0, "resolved": 0, "unverifiable": 0}
        for issue in self.project.issues.values():
            if issue.status not in ("open", "fixed"):
                continue
            ev = (issue.evidence or "").strip()
            find = str((issue.location or {}).get("find") or "").strip()

            # 两个锚点任一仍在文中，就说明问题还在。只看其中一个会漏判：
            # 有的检查器把 evidence 写成整句、location.find 写成短串。
            probes = [p for p in (find, ev) if p]

            if probes:
                if any(p in text for p in probes):
                    census["still_present"] += 1
                    issue.status = "open"
                    issue.resolution_note = ""
                    continue
                # Evidence gone: the defect it pointed at is no longer present.
                if issue.status == "open":
                    issue.status = "fixed"
                    issue.resolution_note = issue.resolution_note or "复验时已无法定位到该问题"
                census["resolved"] += 1
            else:
                # No evidence to re-check. Keep whatever the fixer claimed but
                # mark it unverifiable rather than silently counting it as fixed.
                #
                # INFO 级除外：那是"某项检查被跳过"这类提示，把它塞进"需要你判断"
                # 的队列只会稀释真正要作者行动的问题。
                census["unverifiable"] += 1
                if (
                    issue.status == "open"
                    and not issue.fixable
                    and issue.severity is not Severity.INFO
                ):
                    issue.status = "needs_author"
        return census

    # -- 修复 -------------------------------------------------------------
    def add_patch(self, patch: Patch) -> None:
        self.project.patches[patch.id] = patch

    def mark_fixed(self, issue_ids: Iterable[str], *, by: str, stage: str, note: str = "") -> None:
        for iid in issue_ids:
            issue = self.project.issues.get(iid)
            if issue is None:
                continue
            issue.status = "fixed"
            issue.resolved_by = by
            issue.resolved_at_stage = stage
            issue.resolution_note = note
