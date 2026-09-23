from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "validate_agent_skills.py"
CANONICAL_SKILLS = {
    name: ROOT / "agent-skill" / "skills" / name / "SKILL.md"
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    )
}
CANONICAL_CONTRACT = ROOT / "docs" / "agent-tool-contract.md"
CODEX_INTEGRATION = ROOT / "host-integrations" / "codex"


def run_validator(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_canonical_skill_uses_only_basic_frontmatter() -> None:
    for name, path in CANONICAL_SKILLS.items():
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert lines[0] == "---"
        assert lines[1] == f"name: {name}"
        assert lines[2].startswith("description: ")
        assert lines[3] == "---"
        lowered = content.lower()
        assert ".codex-plugin" not in lowered
        assert "${claude_" not in lowered
        assert "workbuddy" not in lowered


def test_validator_generates_and_checks_a_traceable_workflow(tmp_path: Path) -> None:
    generated_root = tmp_path / "host-package"

    generated = run_validator("--output", str(generated_root))
    checked = run_validator("--output", str(generated_root), "--check")
    assert generated.returncode == 0, generated.stderr
    assert checked.returncode == 0, checked.stderr
    for name, canonical_skill in CANONICAL_SKILLS.items():
        generated_skill = generated_root / "skills" / name / "SKILL.md"
        generated_contract = generated_skill.parent / "references" / "tool-contract.md"
        assert generated_skill.read_text(encoding="utf-8") == canonical_skill.read_text(
            encoding="utf-8"
        )
        assert generated_contract.read_bytes() == CANONICAL_CONTRACT.read_bytes()


def test_revise_skill_enforces_the_w4_e3_confirmation_workflow() -> None:
    content = CANONICAL_SKILLS["revise-roughcut"].read_text(encoding="utf-8")

    for required in (
        "Agent 的初稿局部改稿",
        "初稿页面机械调整",
        "粗剪预览与采用",
        "不得用 Edit 工具绕过初稿确认",
        "只提供连续文稿定位、返回初稿和采用",
        "不在粗剪页重复",
        "返回初稿",
        "旧粗剪失效",
        "复制给 Agent 调整",
        "不得扫描目录",
        "stale",
        "不自动重放、合并或覆盖",
    ):
        assert required in content
    for forbidden in (
        "拖拽",
        "段落手柄",
        "前后移动按钮",
        "右键菜单",
        "自动 filler-word 删除",
        "自由改写同期声",
    ):
        assert forbidden in content


def test_multicam_skill_ownership_and_confirmation_order_are_canonical() -> None:
    basics = CANONICAL_SKILLS["roughcut-basics"].read_text(encoding="utf-8")
    create = CANONICAL_SKILLS["create-roughcut"].read_text(encoding="utf-8")
    render = CANONICAL_SKILLS["render-roughcut"].read_text(encoding="utf-8")

    for required in (
        "只有用户确认的主收声/内容主线机位进入本轮内容整理和 ASR",
        "副机位虽已导入 Project",
        "不得加入 `source_authorizations`",
        "声音对轨直接读取授权的原 Source 音轨，不依赖副机位 Transcript",
        "若首个 Decision 前发现当前 scope 已误含副机位",
        "不得为\n   满足旧 scope 先转录副机位",
        "多机位副机位不在此循环中，不调用",
        "ASR source scope 与 speaker diarization 是两个独立维度",
        "绝不能增加、替换或扩大 source scope",
        "多机位先确定主收声 scope，再只对这个 scope 应用 diarization",
        "不得逐素材重复询问",
        "只对已选主收声 Source 生效",
    ):
        assert required in basics
    assert "不得声称副机位转录是自动选镜、自动切机或声音对轨的前置条件" in basics
    assert "说话人区分只是主收声 Source 的参数" in create
    assert "不会把副机位加入 ASR 范围" in create
    assert "副机位不做 ASR、不进入内容整理" in create
    assert "保持其原始顺序" in create
    assert "不得排序、按 local speaker ID 重建" in create
    assert '`main_camera.camera_id` 必须精确使用协议 ID\n   `"main"`' in create

    assert "multicam_parallel_render_prepare" not in create
    assert "multicam_parallel_render_start" not in create
    assert "`align_multicam`" in create
    assert "`align_multicam`" not in render
    assert create.index("收集并向用户逐项回读主机位、副机位分组和素材授权") < create.index(
        "`workflow_action(adopt_roughcut)`"
    )
    assert create.index("`workflow_action(adopt_roughcut)`") < create.index(
        "Host 预持有新的"
    )
    assert create.index("Host 预持有新的") < create.index("`align_multicam`")
    assert create.index("`align_multicam`") < create.index(
        "`media_operation_status`"
    )
    assert "分组确认只保存\n授权，不调用对轨" in create

    branch = render.split("### 多机位平行输出独立分支", maxsplit=1)[1].split(
        "\n1. ", maxsplit=1
    )[0]
    ordered = (
        "exact succeeded alignment",
        "core coverage",
        "`auxiliary_camera_ids`",
        "`multicam_parallel_render_prepare`",
        "黑画/数字静音",
        "分开的明确批准",
        "Host 才预持有新的 parallel operation ID",
        "`multicam_parallel_render_start`",
        "`media_operation_status`",
    )
    positions = [branch.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert "不启动、重启或补跑对轨" in branch
    assert "不自算 eligibility/slot/quota/hash" in branch
    assert "不自动重试" in branch


def test_qwen_wp1_marker_and_explicit_metadata_rules_are_canonical() -> None:
    basics = CANONICAL_SKILLS["roughcut-basics"].read_text(encoding="utf-8")

    for required in (
        'Path(original_input_filename).stem.endswith("__方言")',
        "原始输入 basename",
        "resolved target filename",
        "可变 `display_name`",
        "fuzzy match",
        "marker 不得改写",
        "用户明确说某个 Source“有方言”或“走在线转录”",
        "先 `project_open` 回读当前",
        "全部\n  `tags`",
        "`source_metadata_update`",
        "完整 tags replacement 和 current expected_revision",
        "省略 `display_name` 以保留当前名称",
        "只 merge 或明确 remove `asr:cloud`",
        "Cloud、credential 或 provider/network failure 自动删除 marker",
        "stale 或 revision conflict",
        "不重放旧写入",
        "当前明确批次",
        "当前 Project 已有 Sources",
        "逐 Source 按顺序执行 read → preserve → merge/remove only",
        "每次成功后继续使用最新 revision",
        "always_use_cloud",
        "default_backend",
        "未来新导入素材自动继承 marker",
        "WP1/WP3A 登记的是 canonical routing metadata 与本地 credential readiness",
        "`asr:cloud` 只表示 route",
        "不等于上传授权",
        "不 fallback 到 local FunASR",
        "保留该 Source 的 `asr:cloud` marker",
        "不增加第二个 public API",
    ):
        assert required in basics

    for forbidden in ("--backend", "--cloud", "--qwen", "--dialect"):
        assert forbidden not in basics


def test_qwen_wp3b_cloud_execution_and_disclosure_are_canonical() -> None:
    basics = CANONICAL_SKILLS["roughcut-basics"].read_text(encoding="utf-8")

    for required in (
        # The one existing confirmation gate carries the third-party disclosure.
        "如果当前 scope 含有",
        "标记为方言/在线转录的素材会将处理后的音频发送到阿里云 Qwen",
        "会产生网络传输、第三方云处理和相应费用",
        "该披露必须在任何上传之前出现",
        "不新增逐文件",
        "也不新增第二套 Cloud 授权状态",
        "绝不能把旧的 local ASR 批准当作 Cloud 上传授权",
        # A configured credential continues through the existing public tool.
        "readiness 为 `configured`",
        "像普通素材一样调用",
        "**原有的 `transcribe_source`**",
        "因为不存在这个参数",
        "Core 自己按 Source 的 canonical route 解析出 Cloud 执行",
        # Not configured stops at setup/readiness, not at a transcription failure.
        "停止该 Source 的转录并进入 credential setup/readiness",
        "用 `qwen_credential_configure` 写入并只回读 non-secret readiness",
        "不额外制造第二个用户确认门",
        "此时不创建",
        "transcription operation",
        # Never a second backend, never a local fallback, never a marker removal.
        "不 fallback 到 local FunASR 或其他 local backend 顶替",
        "不因 credential、provider 或 network failure 移除",
        "不自动重试",
        "不复用旧 operation ID 重跑",
    ):
        assert required in basics

    # The executed rule replaces the old "chain not enabled" stop condition,
    # and the Cloud route never asks for a second per-file confirmation gate.
    for obsolete in (
        "当前 Core 的在线转录执行链尚未启用",
        "不对该 Source 调用 `transcribe_source`",
        "不声称已经上传或即将上传阿里云 Qwen",
    ):
        assert obsolete not in basics


def test_all_canonical_skill_contracts_are_byte_identical() -> None:
    expected = CANONICAL_CONTRACT.read_bytes()
    contract = expected.decode("utf-8")

    assert "Tool schema version: 32" in contract
    assert "`minItems: 1` with no `maxItems`" in contract
    for path in CANONICAL_SKILLS.values():
        assert (path.parent / "references" / "tool-contract.md").read_bytes() == expected


def test_qwen_wp3a_credential_readiness_rules_are_canonical() -> None:
    basics = CANONICAL_SKILLS["roughcut-basics"].read_text(encoding="utf-8")

    for required in (
        "`qwen_credential_configure` / `qwen_credential_readiness` / `qwen_credential_clear`",
        "不回显、不回抄",
        "两个字段必须同时提供",
        "成功后 readback 只看",
        "不得用 CLI argv、环境变量或手工写文件",
        "`not_configured` / `configured` / `invalid` / `insecure`",
        "不得自行修改文件权限",
        "只删除 Qwen credential 记录",
        "readiness 是纯本地读取，不代表 provider 已接受该 key",
        "不得含控制字符、换行/制表符或其他不可见字符",
    ):
        assert required in basics

    # WP3B wires the execution chain, so `configured` no longer means "not
    # enabled": the disclosure WP3A deliberately withheld is now required at the
    # one existing confirmation gate.
    assert "`configured` 只是本地状态" in basics
    assert "配置 credential 不等于启用 Cloud transcription" not in basics


def test_qwen_wp3b_cloud_stop_and_execution_order_is_canonical() -> None:
    basics = CANONICAL_SKILLS["roughcut-basics"].read_text(encoding="utf-8")

    for required in (
        "**Lazy automatic readiness（Cloud scope 边界）**",
        "当且仅当当前显式 ASR/content scope 含有 `asr:cloud`",
        "且 Agent 正在准备该 Source 的 Cloud transcription 时",
        "必须先做一次",
        "`qwen_credential_readiness` 本地读取",
        "这是 readiness 的唯一自动触发边界",
        "以下情况一律不检查、不要求、不报告 credential",
        "`roughcut health`",
        "普通 `diagnostics`",
        "local FunASR/Paraformer transcription",
        "没有 `asr:cloud` Source 的",
        "`project_open`、metadata edit",
        "render/review/NLE",
        "credential 不得成为全局启动门",
        "不把 Cloud readiness 当成 health gate",
        "先按上面的 lazy 规则完成一次 `qwen_credential_readiness` 本地读取",
        "并把 non-secret 状态作为报告的一部分",
        "不是 transcription 失败",
        "不得据此创建 `transcription_failed` MediaOperation",
    ):
        assert required in basics

    # The lazy readiness read must be listed as the first step, before the
    # setup/readiness stop, which itself precedes the executed Cloud route.
    branch = basics[basics.index("若当前 ASR/content scope 含有 `asr:cloud` Source") :]
    step_order = [
        "先按上面的 lazy 规则完成一次",
        "停止该 Source 的转录并进入 credential setup/readiness",
        "像普通素材一样调用",
        "不 fallback 到 local FunASR",
        "保留该 Source 的 `asr:cloud` marker",
        "不自动重试",
    ]
    positions = [branch.index(value) for value in step_order]
    assert positions == sorted(positions)


def test_validator_rejects_a_handwritten_host_integration_skill() -> None:
    with tempfile.TemporaryDirectory(
        dir=CODEX_INTEGRATION, prefix=".skill-copy-test-"
    ) as temporary_dir:
        copied_skill = Path(temporary_dir) / "SKILL.md"
        copied_skill.write_text("---\nname: copied\ndescription: copied\n---\n", encoding="utf-8")

        result = run_validator("--check")

        assert result.returncode == 1
        assert "unexpected handwritten workflow copies" in result.stderr
    assert not Path(temporary_dir).exists()
