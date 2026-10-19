#!/usr/bin/env python3
"""Export or resolve a media-free Roughcut transcript handoff package.

All canonical reads/export/resolution go through an installed absolute Roughcut
CLI. This helper never imports Roughcut code and never writes Project JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

FORMAT_VERSION = 1
NOTE_FILE = "handoff.md"
TRANSCRIPT_FILE = "transcript.md"
MAP_FILE = "transcript.map.json"
P_NUMBER = re.compile(r"P[0-9]{3,}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PAGE_LIMIT = 200
RULE_SOURCE = "引用真实同期声时，保留本包 P### 编号和连续原文；不改写、不纠错、不补省略号。"
RULE_REPEAT = "同一 P 内 quote 重复出现时必须标明第几次出现（occurrence 从 0 开始）；引用整段时省略 quote。"
RULE_SPLIT = "一个 source_excerpt 只能定位一个 P；跨 P、跳段或删除中间内容时必须拆成多条引用。"
RULE_NO_TIMECODE = "不要填写或推算 ticks、refs、字符 offset 或时码。"
RULE_UNCERTAIN = "无法确定的素材请标“待核实”，不要猜。"
RULE_FINAL = (
    "用户确认定稿后，只按 handoff.md 的模板输出一份包含 package_id 和有序 blocks 的 JSON；"
    "讨论/创作过程可以用自然语言，不必只输出 JSON。"
)


class HandoffError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _fail(code: str, message: str, **details: Any) -> NoReturn:
    raise HandoffError(code, message, **details)


def _run_cli(cli: Path, arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [str(cli), *arguments, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    if completed.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True:
        return payload
    code, detail = "cli_error", completed.stderr.strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            code = error["code"]
        detail = detail or str(payload.get("error", ""))
    _fail(code, f"Roughcut CLI {arguments[0]} failed: {detail or 'unknown error'}")


def _cli_path(value: str) -> Path:
    cli = Path(value)
    if not cli.is_absolute() or not cli.is_file():
        _fail("cli_path_invalid", "--cli must be the absolute path of an installed roughcut CLI")
    return cli


def _workflow(cli: Path, project: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _run_cli(cli, ["workflow-status", "--project", str(project)])
    status = payload.get("status")
    run = status.get("workflow_run") if isinstance(status, dict) else None
    if (
        not isinstance(run, dict)
        or run.get("lifecycle") != "active"
        or not isinstance(run.get("run_id"), str)
        or not run["run_id"]
    ):
        _fail("workflow_required", "workflow-status returned no active WorkflowRun")
    return status, run


def _bindings(run: dict[str, Any]) -> list[dict[str, str]]:
    ordered = run.get("ordered_bindings")
    if not isinstance(ordered, list):
        _fail("workflow_invalid", "WorkflowRun has no ordered bindings")
    bindings: list[dict[str, str]] = []
    for item in ordered:
        if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
            _fail("workflow_invalid", "WorkflowRun binding is invalid")
        source_id = item["source_id"]
        transcript_id = item.get("transcript_version_id")
        if not isinstance(transcript_id, str) or not transcript_id:
            _fail(
                "transcript_not_ready",
                f"ordered content binding {source_id} has no ready Transcript; "
                "finish transcription or explicitly change scope before export",
                source_id=source_id,
            )
        bindings.append({"source_id": source_id, "transcript_version_id": transcript_id})
    if not bindings:
        _fail("workflow_invalid", "WorkflowRun has no exact transcript bindings")
    return bindings


def _project(cli: Path, project: Path) -> dict[str, Any]:
    payload = _run_cli(cli, ["project-open", "--project", str(project)])
    data = payload.get("project")
    if not isinstance(data, dict) or not isinstance(data.get("revision"), int):
        _fail("handoff_invalid", "project-open returned an invalid project")
    return data


def _readable(
    cli: Path, project: Path, bindings: list[dict[str, str]], revision: int
) -> dict[str, Any]:
    offset, paragraphs, frozen = 0, [], None
    while True:
        payload = _run_cli(
            cli,
            [
                "readable-transcript-read", "--project", str(project),
                "--source-bindings-json", json.dumps(bindings, ensure_ascii=False),
                "--expected-revision", str(revision), "--offset", str(offset),
                "--limit", str(PAGE_LIMIT),
            ],
        )
        view = payload.get("readable_transcript")
        if not isinstance(view, dict) or not isinstance(view.get("paragraphs"), list):
            _fail("handoff_invalid", "readable-transcript-read returned an invalid view")
        current = {
            "view_hash": view.get("view_hash"),
            "project_id": view.get("project_id"),
            "project_revision": view.get("project_revision"),
            "source_bindings": view.get("source_bindings"),
        }
        if not isinstance(current["view_hash"], str) or not isinstance(
            current["project_id"], str
        ) or not isinstance(current["project_revision"], int) or not isinstance(
            current["source_bindings"], list
        ):
            _fail("handoff_invalid", "readable transcript view is invalid")
        if frozen is None:
            frozen = current
        elif current != frozen:
            _fail("package_stale", "readable transcript changed while it was being read")
        paragraphs.extend(view["paragraphs"])
        cursor = view.get("next_cursor")
        if cursor is None:
            break
        if not isinstance(cursor, int) or cursor <= offset:
            _fail("handoff_invalid", "readable transcript cursor is invalid")
        offset = cursor
    if frozen is None or not paragraphs:
        _fail("handoff_invalid", "readable transcript is empty")
    if frozen["source_bindings"] != bindings:
        _fail("workflow_invalid", "readable transcript did not use the exact ordered bindings")
    return {**frozen, "paragraphs": paragraphs}


def _brief(cli: Path, project: Path, project_data: dict[str, Any]) -> dict[str, Any] | None:
    brief_id = project_data.get("active_brief_id")
    if not isinstance(brief_id, str) or not brief_id:
        return None
    payload = _run_cli(cli, ["brief-read", "--project", str(project), "--brief-id", brief_id])
    brief = payload.get("brief")
    if not isinstance(brief, dict) or brief.get("brief_id") != brief_id:
        _fail("handoff_invalid", "brief-read returned an unexpected Brief")
    return brief


def _outline(run: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    refs = run.get("artifact_refs")
    outline = refs.get("outline") if isinstance(refs, dict) else None
    if not isinstance(outline, dict) or not isinstance(outline.get("snapshot"), dict):
        return None, None
    return outline["snapshot"], outline


def _approval(value: object, present: bool) -> str:
    if not present:
        return "未提供/待讨论"
    return "已批准" if value == "current" else "已提交，尚未批准"


def _note(
    metadata: dict[str, Any],
    brief: dict[str, Any] | None,
    brief_status: object,
    outline: dict[str, Any] | None,
    outline_status: object,
    background: str | None,
) -> str:
    if brief is None:
        brief_text = f"未提供/待讨论（Brief 状态：{_approval(brief_status, False)}）"
    else:
        brief_text = (
            f"- 状态：{_approval(brief_status, True)}\n"
            f"- 主题：{brief.get('theme', '')}\n"
            f"- 目标时长：{brief.get('target_duration_ticks')} ticks\n"
            f"- 建议保留/重点：{'、'.join(brief.get('focus') or [])}\n"
            f"- 允许按主题重组：{'是' if brief.get('allow_reorder') else '否'}"
        )
    if outline is None:
        outline_text = f"未提供/待讨论（Outline 状态：{_approval(outline_status, False)}）"
    else:
        outline_text = (
            f"- 状态：{_approval(outline_status, True)}\n"
            f"```json\n{json.dumps(outline, ensure_ascii=False, indent=2)}\n```"
        )
    example = (
        '{\n  "package_id": "<handoff.md 中的 package_id>",\n  "blocks": [\n'
        '    {"section_title": "开场"},\n'
        '    {"narration": "未录解说文字。"},\n'
        '    {"source_excerpt": {"paragraph": "P001"}},\n'
        '    {"source_excerpt": {"paragraph": "P003", "quote": "重复出现的完整原话", "occurrence": 1}}\n'
        "  ]\n}"
    )
    return f"""---
{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}
---

# Roughcut 外部定稿交接

## 包标识与依据

- package_id：`{metadata['package_id']}`
- project_id：`{metadata['project_id']}`
- project_revision：`{metadata['project_revision']}`
- view_hash：`{metadata['view_hash']}`
- workflow_run_id：`{metadata['workflow_run_id']}`
- 源绑定：`{json.dumps(metadata['source_bindings'], ensure_ascii=False)}`

## 剪辑要求

{brief_text}

## 已有大纲

{outline_text}

## 用户指定背景资料

{background or '未提供（不得自行遍历目录或补充背景资料）'}

## 交接规则

以下约束针对最终交接稿；讨论过程可以使用自然语言。

1. 最终交接稿只返回文稿内容与顺序，不输出媒体路径或项目内部字段。
2. `P###` 只使用 {TRANSCRIPT_FILE} 标题中的本包编号。{RULE_SOURCE}
3. {RULE_REPEAT}
4. {RULE_SPLIT}
5. 标题用独立 section_title，未录解说用独立 narration；最终顺序按 blocks 顺序。{RULE_NO_TIMECODE}
6. {RULE_FINAL}

## 最终输出模板

```json
{example}
```

导回后仍由 Roughcut 校验当前 Brief、Outline、上下文和 Draft 门禁；本包不写入项目。
"""


def _forward_message(metadata: dict[str, Any], paragraph_count: int, source_count: int) -> str:
    return f"""我已导出当前粗剪项目的转录交接包，供你参考组稿/改稿。

包标识（package_id）：{metadata['package_id']}
本包含 {source_count} 个转录源、{paragraph_count} 段。
请一起接收三个文件：transcript.md、transcript.map.json、handoff.md，并先读 handoff.md；项目、剪辑要求、已有大纲、背景资料和最终输出模板都在其中。

章节结构和解说可以自由创作、修改；只有真实同期声需要严格保留原文。讨论过程可以用自然语言，不必只输出 JSON。引用真实同期声时：
- {RULE_SOURCE}
- {RULE_REPEAT}
- {RULE_SPLIT}
- {RULE_NO_TIMECODE}
- {RULE_UNCERTAIN}

{metadata['package_id']} 是本包唯一标识，请勿跨包混用 P 编号。{RULE_FINAL}"""


def _validate_map(mapping: object, view: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        _fail("map_invalid", "mapping file is not a JSON object")
    if mapping.get("schema_version") != 1 or mapping.get("basis") != "transcript":
        _fail("map_invalid", "mapping file has an unsupported basis")
    if mapping.get("view_hash") != view["view_hash"]:
        _fail("package_stale", "mapping view hash does not match the transcript")
    if mapping.get("source_bindings") != view["source_bindings"]:
        _fail("package_stale", "mapping source bindings do not match the transcript")
    entries = mapping.get("entries")
    paragraphs = view["paragraphs"]
    if not isinstance(entries, list) or len(entries) != len(paragraphs):
        _fail("map_invalid", "mapping entries do not cover the transcript")
    numbers, paragraph_ids = set(), set()
    for entry, paragraph in zip(entries, paragraphs):
        if not isinstance(entry, dict) or set(entry) != {"paragraph_id", "display_number", "refs"}:
            _fail("map_invalid", "mapping entry fields are invalid")
        number, paragraph_id = entry.get("display_number"), entry.get("paragraph_id")
        if (
            not isinstance(number, str) or P_NUMBER.fullmatch(number) is None
            or number in numbers or not isinstance(paragraph_id, str)
            or paragraph_id in paragraph_ids
        ):
            _fail("map_invalid", "mapping entry identity is invalid")
        if paragraph_id != paragraph.get("paragraph_id") or entry.get("refs") != paragraph.get("refs"):
            _fail("map_invalid", f"mapping entry {number} does not match the transcript")
        numbers.add(number)
        paragraph_ids.add(paragraph_id)
    return mapping


def _package_hash(markdown: bytes, mapping: bytes) -> str:
    return hashlib.sha256(markdown + b"\0" + mapping).hexdigest()


def export_package(args: argparse.Namespace) -> dict[str, Any]:
    cli = _cli_path(args.cli)
    project, package = Path(args.project).resolve(), Path(args.package).resolve()
    if package.exists():
        _fail("package_exists", f"package path already exists: {package}")
    project_data = _project(cli, project)
    status, run = _workflow(cli, project)
    if run.get("project_id") != project_data.get("project_id"):
        _fail("project_mismatch", "WorkflowRun belongs to another project")
    bindings = _bindings(run)
    view = _readable(cli, project, bindings, project_data["revision"])
    brief = _brief(cli, project, project_data)
    outline, outline_ref = _outline(run)
    approvals = status.get("approval_statuses")
    approvals = approvals if isinstance(approvals, dict) else {}
    background = None
    if args.background:
        try:
            background = Path(args.background).read_text(encoding="utf-8").strip()
        except OSError as error:
            _fail("handoff_invalid", f"background file cannot be read: {error}")

    package.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{package.name}.tmp-", dir=package.parent))
    try:
        exported = _run_cli(
            cli,
            [
                "markdown-export", "--project", str(project), "--basis", "transcript",
                "--output", str(temporary / TRANSCRIPT_FILE),
                "--expected-revision", str(view["project_revision"]),
                "--source-bindings-json", json.dumps(view["source_bindings"], ensure_ascii=False),
            ],
        )
        result = exported.get("markdown_export")
        if not isinstance(result, dict) or not isinstance(result.get("content_hash"), str):
            _fail("cli_error", "markdown-export returned no content hash")
        content_hash = result["content_hash"]
        try:
            map_bytes = (temporary / MAP_FILE).read_bytes()
            mapping = json.loads(map_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            _fail("map_invalid", "mapping file is unreadable")
        _validate_map(mapping, view)
        if _package_hash((temporary / TRANSCRIPT_FILE).read_bytes(), map_bytes) != content_hash:
            _fail("package_tampered", "export content hash does not match its files")
        metadata = {
            "format": "roughcut-external-handoff",
            "format_version": FORMAT_VERSION,
            "package_id": content_hash,
            "project_id": view["project_id"],
            "project_revision": view["project_revision"],
            "view_hash": view["view_hash"],
            "source_bindings": view["source_bindings"],
            "brief_id": brief.get("brief_id") if brief else None,
            "workflow_run_id": run.get("run_id"),
            "outline_artifact_id": outline_ref.get("artifact_id") if outline_ref else None,
            "outline_content_hash": outline_ref.get("content_hash") if outline_ref else None,
            "outline_approval_status": approvals.get("outline"),
        }
        (temporary / NOTE_FILE).write_text(
            _note(
                metadata, brief, approvals.get("brief"), outline,
                approvals.get("outline"), background,
            ),
            encoding="utf-8",
        )
        if package.exists():
            _fail("package_exists", f"package path already exists: {package}")
        os.replace(temporary, package)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "ok": True,
        "package_id": content_hash,
        "package_path": str(package),
        "files": [TRANSCRIPT_FILE, MAP_FILE, NOTE_FILE],
        "project_id": view["project_id"],
        "project_revision": view["project_revision"],
        "view_hash": view["view_hash"],
        "workflow_run_id": run.get("run_id"),
        "stage": run.get("stage"),
        "outline_source": "workflow_status" if outline else "none",
        "forward_message": _forward_message(
            metadata, len(view["paragraphs"]), len(metadata["source_bindings"])
        ),
    }


def _parse_note(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _fail("package_missing", "handoff note is unreadable")
    if not text.startswith("---\n"):
        _fail("handoff_invalid", "handoff note has no metadata block")
    end = text.find("\n---\n", 4)
    if end < 0:
        _fail("handoff_invalid", "handoff note metadata block is not closed")
    try:
        metadata = json.loads(text[4:end])
    except json.JSONDecodeError:
        _fail("handoff_invalid", "handoff note metadata is invalid")
    required = {
        "format", "format_version", "package_id", "project_id", "project_revision",
        "view_hash", "source_bindings", "workflow_run_id",
    }
    if not isinstance(metadata, dict) or not required <= set(metadata):
        _fail("handoff_invalid", "handoff note metadata is incomplete")
    if metadata["format"] != "roughcut-external-handoff" or metadata["format_version"] != FORMAT_VERSION:
        _fail("handoff_invalid", "handoff note format is unsupported")
    if not isinstance(metadata["package_id"], str) or SHA256.fullmatch(metadata["package_id"]) is None:
        _fail("handoff_invalid", "package_id is invalid")
    if not isinstance(metadata["view_hash"], str) or SHA256.fullmatch(metadata["view_hash"]) is None:
        _fail("handoff_invalid", "view_hash is invalid")
    if not isinstance(metadata["project_id"], str) or not isinstance(metadata["workflow_run_id"], str):
        _fail("handoff_invalid", "project or workflow run ID is invalid")
    if (
        isinstance(metadata["project_revision"], bool)
        or not isinstance(metadata["project_revision"], int)
        or metadata["project_revision"] < 0
    ):
        _fail("handoff_invalid", "project_revision is invalid")
    bindings = metadata["source_bindings"]
    if not isinstance(bindings, list) or not bindings:
        _fail("handoff_invalid", "source_bindings are invalid")
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"source_id", "transcript_version_id"}
            or not all(isinstance(binding[key], str) and binding[key] for key in binding)
        ):
            _fail("handoff_invalid", "source_bindings are invalid")
    return metadata


def _load_draft(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("draft_invalid", "draft JSON is unreadable")
    if not isinstance(data, dict) or set(data) != {"package_id", "blocks"}:
        _fail("draft_invalid", "draft must contain exactly package_id and blocks")
    if not isinstance(data["package_id"], str) or SHA256.fullmatch(data["package_id"]) is None:
        _fail("draft_invalid", "draft package_id is invalid")
    if not isinstance(data["blocks"], list) or not data["blocks"]:
        _fail("draft_invalid", "draft blocks must be a non-empty array")
    return data


def _draft_block(index: int, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) != 1:
        _fail("draft_invalid", f"blocks[{index}] must contain exactly one block kind")
    kind, payload = next(iter(value.items()))
    if kind == "section_title":
        if not isinstance(payload, str) or not payload.strip():
            _fail("draft_invalid", f"blocks[{index}].section_title is empty")
        return {"kind": kind, "title": payload.strip()}
    if kind == "narration":
        if not isinstance(payload, str) or not payload.strip():
            _fail("draft_invalid", f"blocks[{index}].narration is empty")
        return {"kind": kind, "text": payload.strip()}
    if kind != "source_excerpt" or not isinstance(payload, dict):
        _fail("draft_invalid", f"blocks[{index}] has an unsupported block")
    if not set(payload) <= {"paragraph", "quote", "occurrence"} or "paragraph" not in payload:
        _fail("draft_invalid", f"blocks[{index}].source_excerpt fields are invalid")
    paragraph, quote, occurrence = payload["paragraph"], payload.get("quote"), payload.get("occurrence")
    if not isinstance(paragraph, str) or P_NUMBER.fullmatch(paragraph) is None:
        _fail("draft_invalid", f"blocks[{index}].paragraph must be P###")
    if quote is not None and (not isinstance(quote, str) or not quote):
        _fail("draft_invalid", f"blocks[{index}].quote is empty")
    if occurrence is not None and (
        quote is None or isinstance(occurrence, bool)
        or not isinstance(occurrence, int) or occurrence < 0
    ):
        _fail("draft_invalid", f"blocks[{index}].occurrence is invalid")
    return {"kind": kind, "paragraph": paragraph, "quote": quote, "occurrence": occurrence}


def _occurrences(text: str, quote: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while (position := text.find(quote, cursor)) >= 0:
        positions.append(position)
        cursor = position + 1
    return positions


def _safe_output(project: Path, package: Path, draft: Path, output: Path) -> Path:
    project, package, draft, output = (
        project.resolve(), package.resolve(), draft.resolve(), output.resolve()
    )
    if (
        (output.exists() and not output.is_file())
        or output == draft
        or output.is_relative_to(project)
        or output.is_relative_to(package)
    ):
        _fail(
            "output_unsafe",
            "--output must be a file outside the project and package, and not the draft",
        )
    return output


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def resolve_package(args: argparse.Namespace) -> dict[str, Any]:
    cli = _cli_path(args.cli)
    project, package = Path(args.project).resolve(), Path(args.package).resolve()
    output = _safe_output(project, package, Path(args.draft), Path(args.output))
    if not package.is_dir():
        _fail("package_missing", f"package directory is missing: {package}")
    metadata = _parse_note(package / NOTE_FILE)
    try:
        markdown = (package / TRANSCRIPT_FILE).read_bytes()
        map_bytes = (package / MAP_FILE).read_bytes()
    except OSError:
        _fail("package_missing", "package transcript or map is missing")
    if _package_hash(markdown, map_bytes) != metadata["package_id"]:
        _fail("package_tampered", "package content hash does not match handoff.md; re-export")
    try:
        mapping = json.loads(map_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("map_invalid", "mapping file is unreadable")
    draft = _load_draft(Path(args.draft))
    if draft["package_id"] != metadata["package_id"]:
        _fail("package_id_mismatch", "draft package_id does not match this package")
    parsed = [_draft_block(index, value) for index, value in enumerate(draft["blocks"])]

    project_data = _project(cli, project)
    if project_data.get("project_id") != metadata["project_id"]:
        _fail("project_mismatch", "package belongs to a different project")
    if project_data.get("revision") != metadata["project_revision"]:
        _fail("package_stale", "project revision changed since export")
    _status, run = _workflow(cli, project)
    if run.get("run_id") != metadata["workflow_run_id"]:
        _fail("package_stale", "WorkflowRun changed since export")
    if _bindings(run) != metadata["source_bindings"]:
        _fail("package_stale", "WorkflowRun ordered bindings changed since export")

    view = _readable(cli, project, metadata["source_bindings"], metadata["project_revision"])
    if view["view_hash"] != metadata["view_hash"] or mapping.get("view_hash") != metadata["view_hash"]:
        _fail("package_stale", "transcript view changed since export")
    _validate_map(mapping, view)
    by_number = {entry["display_number"]: entry for entry in mapping["entries"]}
    blocks: list[dict[str, Any]] = []
    try:
        for index, block in enumerate(parsed):
            block_id = f"handoff_block_{index + 1:03d}"
            if block["kind"] == "section_title":
                blocks.append({"block_id": block_id, "kind": "section_title", "title": block["title"]})
                continue
            if block["kind"] == "narration":
                blocks.append({
                    "block_id": block_id, "kind": "narration", "text": block["text"],
                    "status": "draft", "recorded_refs": [],
                })
                continue
            number = block["paragraph"]
            entry = by_number.get(number)
            if entry is None:
                _fail("p_number_unknown", f"{number} is not part of this package", block_index=index, paragraph=number)
            live = next(
                (item for item in view["paragraphs"] if item.get("paragraph_id") == entry["paragraph_id"]),
                None,
            )
            if not isinstance(live, dict) or not isinstance(live.get("text"), str):
                _fail("package_stale", f"{number} no longer exists", block_index=index, paragraph=number)
            quote, occurrence = block["quote"], block["occurrence"]
            selection, requested_start, requested_end = {"paragraph_id": entry["paragraph_id"]}, None, None
            if quote is not None:
                matches = _occurrences(live["text"], quote)
                if not matches:
                    _fail("quote_not_found", f"{number} does not contain the exact quote", block_index=index, paragraph=number)
                if len(matches) > 1 and occurrence is None:
                    _fail(
                        "quote_occurrence_required",
                        f"{number} contains the quote {len(matches)} times; occurrence is required (0-based)",
                        block_index=index, paragraph=number, occurrences=matches,
                    )
                selected = 0 if occurrence is None else occurrence
                if selected >= len(matches):
                    _fail(
                        "occurrence_invalid",
                        f"{number} has only {len(matches)} occurrence(s) (0-based)",
                        block_index=index, paragraph=number, occurrences=matches,
                    )
                requested_start = matches[selected]
                requested_end = requested_start + len(quote)
                selection.update({
                    "start_offset": requested_start, "end_offset": requested_end,
                    "quote": quote, "occurrence": selected,
                })
            try:
                payload = _run_cli(
                    cli,
                    [
                        "transcript-selection-resolve", "--project", str(project),
                        "--source-bindings-json", json.dumps(metadata["source_bindings"], ensure_ascii=False),
                        "--expected-revision", str(metadata["project_revision"]),
                        "--view-hash", metadata["view_hash"],
                        "--selections-json", json.dumps([selection], ensure_ascii=False),
                    ],
                )
            except HandoffError as error:
                _fail(error.code, error.message, block_index=index, paragraph=number)
            resolution = payload.get("transcript_selection")
            if not isinstance(resolution, dict) or resolution.get("view_hash") != metadata["view_hash"]:
                _fail("package_stale", "resolver view hash changed", block_index=index, paragraph=number)
            refs = resolution.get("refs")
            if not isinstance(refs, list) or not refs:
                _fail("cli_error", "resolver returned no exact refs", block_index=index, paragraph=number)
            if resolution.get("mode") == "expanded_to_segments":
                _fail(
                    "expanded_to_segments",
                    "请求的引用缺少可信细粒度时间，工具把实际范围扩大到完整 segment。"
                    "请对照 requested 与 resolved 选择：接受实际范围就把该条 quote 改为"
                    "工具返回的完整原文后重新导回；否则另选引用。不要自行估算或改写。",
                    block_index=index, paragraph=number, requested_quote=quote,
                    requested_start_offset=requested_start, requested_end_offset=requested_end,
                    resolved_canonical_text=resolution.get("canonical_text"),
                    resolved_refs=refs, warnings=resolution.get("warnings"),
                )
            blocks.append({
                "block_id": block_id, "kind": "source_excerpt",
                "refs": [
                    {key: ref[key] for key in (
                        "source_id", "transcript_version_id", "segment_id", "start_ticks", "end_ticks"
                    )}
                    for ref in refs
                ],
            })
    except HandoffError as error:
        entry = {"code": error.code, "message": error.message, **error.details}
        report = {
            "ok": False,
            "package_id": metadata["package_id"],
            "project_id": metadata["project_id"],
            "project_revision": metadata["project_revision"],
            "view_hash": metadata["view_hash"],
            "errors": [] if error.code == "expanded_to_segments" else [entry],
            "expansions": [entry] if error.code == "expanded_to_segments" else [],
            "blocks": [],
        }
        _write_report(output, report)
        return report
    report = {
        "ok": True,
        "format_version": FORMAT_VERSION,
        "package_id": metadata["package_id"],
        "project_id": metadata["project_id"],
        "project_revision": metadata["project_revision"],
        "view_hash": metadata["view_hash"],
        "source_bindings": metadata["source_bindings"],
        "blocks": blocks,
        "notes": [
            "These blocks are a candidate only; no Draft was submitted.",
            "Re-check workflow_status, Brief, Outline and context gates before submit_draft.",
        ],
    }
    _write_report(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--project", required=True)
    export.add_argument("--package", required=True)
    export.add_argument("--cli", required=True)
    export.add_argument("--background")
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--project", required=True)
    resolve.add_argument("--package", required=True)
    resolve.add_argument("--draft", required=True)
    resolve.add_argument("--output", required=True)
    resolve.add_argument("--cli", required=True)
    args = parser.parse_args(argv)

    if args.command == "resolve":
        try:
            _safe_output(Path(args.project), Path(args.package), Path(args.draft), Path(args.output))
        except HandoffError as error:
            print(f"{error.code}: {error.message}", file=sys.stderr)
            return 2
    try:
        result = export_package(args) if args.command == "export" else resolve_package(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 2
    except HandoffError as error:
        if args.command == "resolve":
            try:
                _write_report(
                    Path(args.output),
                    {
                        "ok": False,
                        "errors": [{"code": error.code, "message": error.message, **error.details}],
                        "expansions": [],
                        "blocks": [],
                    },
                )
            except OSError:
                pass
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
