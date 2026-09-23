from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS = {
    name: (ROOT / "agent-skill" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    )
}


def test_unified_roughcut_skill_routes_without_copying_workflow_rules() -> None:
    entry = SKILLS["roughcut"]
    frontmatter = entry.split("---", 2)[1]
    assert "开始粗剪" in frontmatter
    assert "创建粗剪项目" in frontmatter
    assert "继续初稿" in frontmatter
    assert "调整粗剪" in frontmatter
    assert "开始粗剪" in entry
    assert "创建粗剪项目" in entry
    assert "$roughcut" in entry
    assert "/roughcut" in entry
    assert "宿主可选快捷别名" in entry
    for skill in ("roughcut-basics", "create-roughcut", "revise-roughcut", "render-roughcut"):
        assert skill in entry
    assert "source_add" not in entry
    assert "content_draft_create" not in entry
    assert "render_roughcut" not in entry


def test_new_project_material_guidance_is_user_confirmed_and_not_visual_inference() -> None:
    basics = SKILLS["roughcut-basics"]
    for required in (
        "序号_场景或节点_人物或内容_机位或时间",
        "文件名/路径",
        "用户描述",
        "ffprobe",
        "显示名、role tags、其他 tags 和 note",
        "用户提供或明确确认",
        "不得要求视觉模型",
        "不得在 ASR 前自动判断人物、内容、主线/补充角色、叙事价值或音质",
        "确认这些素材并开始准备",
        "直接进入 ASR",
        "代理不是默认动作",
        "`project_open` 实际发现并校验既有 artifact",
    ):
        assert required in basics
    for required in (
        "新项目不要先调用 `project_create`",
        "用户确认精确素材清单和 `output_preset` 后",
        "新项目才调用 `project_create`",
        "已有项目只回读当前 Project settings",
        "`proxy_read` 只用于发现可复用的 ready 代理",
        "不能判断原素材是否需要代理",
        "真实 Review 播放/seek 失败或用户明确反馈",
    ):
        assert required in basics


def test_long_task_guidance_uses_host_managed_session_and_only_fixed_status_copy() -> None:
    combined = "\n".join(SKILLS.values())
    for required in (
        "宿主管理的原生 MCP",
        "task/session",
        "保留句柄",
        "CLI 进程/session",
        "schema 校验或工具调用失败不等于 MCP 不可用",
        "不得改走 CLI",
        "进程存活",
        "已耗时",
        "当前步骤",
        "最终单一 JSON readback",
        "没有可信百分比 API 时严禁伪造百分比",
        "📌 需要你确认",
        "⏳ 正在处理",
        "✅ 已完成",
        "⚠️ 需要处理",
    ):
        assert required in combined
    for fixed_status in (
        "⏳ 等待开始：任务已记录，媒体处理尚未开始。",
        "⏳ 正在处理：任务仍在运行。",
        "✅ 已完成：任务已成功完成。",
        "⚠️ 需要处理：任务执行失败。",
        "⚠️ 已中断：执行进程已经结束，任务没有成功终态。",
    ):
        assert fixed_status in combined
    assert "`media_operation_status`" in combined
    assert "已持有的 operation ID" in combined
    assert "不得自行 `sleep`" in combined
    assert "不得扫描 Transcript、Proxy、Render、staging、receipt" in combined


def test_controlled_fake_long_task_keeps_a_handle_reports_liveness_and_reads_one_json() -> None:
    script = (
        "import json,time; time.sleep(0.05); "
        "print(json.dumps({'schema_version': 1, 'tool_schema_version': 26, "
        "'ok': True, 'result': 'fixture'}))"
    )
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    elapsed = time.monotonic() - started

    assert process.returncode == 0
    assert elapsed >= 0.05
    assert stderr == ""
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout) == {
        "schema_version": 1,
        "tool_schema_version": 26,
        "ok": True,
        "result": "fixture",
    }


def test_create_skill_uses_one_copyable_requirements_template_and_evidence_bound_advice() -> None:
    create = SKILLS["create-roughcut"]
    for required in (
        "主题：",
        "目标时长：X 分 X 秒",
        "风格：",
        "解说方式：",
        "内容顺序：",
        "开场方式：",
        "必须保留 / 必须避免：",
        "请 Agent 建议",
        "完整分页读取 Readable Transcript 后",
        "source/transcript/segment/ticks",
        "缺少可靠金句、结尾或过渡时必须明确说明",
        "不得补写或改写同期声",
        "大纲本身不创建 Proposal、Decision 或 Render",
    ):
        assert required in create
    assert "向用户展示 core" in create
    assert "返回的 exact current 大纲后" in create
    assert "`agent_propose` 必须展示完整大纲并等待用户明确确认" in create
    assert "`user_reference` 若有调整" in create
    assert "展示主要变化和最终大纲并等待确认" in create
    assert "`user_directed` 无实质结构变化时" in create
    assert "不得再问“请确认这个" in create
    assert "这次 directed approval 只跨 Outline gate" in create
    assert "不得跨初稿确认、粗剪采用或正式导出" in create
    assert create.index("workflow_action(approve_outline)") < create.index(
        "workflow_action(submit_draft)"
    )
    for forbidden in ("受众", "发布平台"):
        assert forbidden not in create
