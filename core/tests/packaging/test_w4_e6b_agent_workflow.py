from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS = {
    name: (ROOT / "agent-skill" / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for name in ("roughcut", "create-roughcut", "revise-roughcut")
}


def compact(text: str) -> str:
    return "".join(text.split())


def assert_contains(text: str, phrase: str) -> None:
    assert compact(phrase) in compact(text)


def assert_in_order(text: str, *phrases: str) -> None:
    content = compact(text)
    position = -1
    for phrase in phrases:
        phrase = compact(phrase)
        position = content.find(phrase, position + 1)
        assert position >= 0, f"missing {phrase!r}"


def test_create_workflow_requires_outline_gate_before_every_content_draft() -> None:
    router = SKILLS["roughcut"]
    create = SKILLS["create-roughcut"]

    for content in (router, create):
        assert_contains(content, "单素材")
        assert_contains(content, "顺序明确")
    assert_contains(router, "不得跳过大纲")
    assert_contains(create, "不得因单素材或 Agent 认为顺序明确而省略 formal Outline artifact")

    for required in (
        "剪辑要求摘要",
        "不在这里提前提出开头、主体、结尾或大纲",
        "简短标题候选",
        "开头",
        "普通 interview-v1 的 Agent 自主建议通常约 4–7 个主体章节",
        "用户自带或内容需要的结构只要求非空",
        "结尾",
        "大致时长分配",
        "必须保留内容覆盖",
        "解说状态",
        "确认、重排、增删章节",
        "改变开头或结尾",
        "要求另一版",
        "按推荐方案出稿",
        "金句快剪只确认形式",
        "候选金句必须来自真实 exact refs",
        "反复替换",
        "不得自动定稿或虚构",
        "大纲本身不创建 Proposal、Decision 或 Render",
        "`display_title`",
        "`section_title`",
    ):
        assert_contains(create, required)

    assert_in_order(
        create,
        "剪辑要求摘要",
        "`workflow_action(confirm_brief)`",
        "`agent_context`",
        "建立正式大纲",
        "`workflow_action(submit_outline)`",
        "`workflow_action(approve_outline)`",
        "`workflow_action(submit_draft)`",
    )


def test_create_workflow_distinguishes_outline_policy_scenarios() -> None:
    create = SKILLS["create-roughcut"]

    # Scenario 1: Agent designs the structure and must wait.
    assert_contains(create, "`agent_propose`")
    assert_contains(create, "完整展示大纲并等待用户确认")
    assert_contains(create, "不得自动调用 `approve_outline`")

    # Scenario 2: a reference outline may change, but is not prior approval.
    assert_contains(create, "`user_reference`")
    assert_contains(create, "明确回报主要结构变化")
    assert_contains(create, "reference 不是 approval")

    # Scenarios 3 and 4: directed 3/9-section structures keep count and order.
    assert_contains(create, "`user_directed`")
    assert_contains(create, "保留用户章节数量与顺序")
    assert_contains(create, "不得把 3 章拆成 4 章")
    assert_contains(create, "不得把 9 章合并到 7 章")
    assert_contains(create, "不再询问“请确认这个大纲”")

    # Scenario 5: insufficient evidence is disclosed without fabrication.
    assert_contains(create, "素材不足时仍保留该章")
    assert_contains(create, "不得删除/合并该章、虚构内容或机械重复素材")

    # Scenario 6: material structural changes return to confirmation.
    assert_contains(create, "以下均为实质变化")
    assert_contains(create, "必须展示变化并等待用户确认后才")
    assert_contains(create, "原始 `user_directed` 不是无限调整授权")

    directed = create.split("- `user_directed`：", maxsplit=1)[1].split(
        "\n\n机械正规化", maxsplit=1
    )[0]
    assert_in_order(directed, "`submit_outline`", "`approve_outline`", "生成 Draft")
    assert "等待用户确认" not in directed


def test_revise_workflow_enforces_scoped_immutable_child_rounds() -> None:
    revise = SKILLS["revise-roughcut"]

    for required in (
        "不限次数",
        "重做开头或结尾",
        "替换金句",
        "增删或重排章节",
        "压缩某一章节",
        "补找遗漏内容",
        "平衡人物或素材",
        "调整解说策略或时长分配",
        "自然语言",
        "不要求先在页面选中",
        "复述它理解的修改范围",
        "只询问一个真正阻塞的问题",
        "当前 Review session 指向或前一轮工具返回的精确 candidate ID",
        "不得扫描文件或按时间猜测“最新稿”",
        "第一段",
        "首次解释相对指代时",
        "candidate ID、目标 block IDs/exact refs 和短引文",
        "写入前重新读取",
        "逐项比较冻结目标",
        "原目标已变化",
        "删除、移动或替换",
        "停止并只询问一个确认问题",
        "不得把原命令静默重绑到当前下一段",
            "以前一轮返回的精确 child 为 parent",
            "`workflow_action(submit_draft)`",
        "block ID、exact refs、顺序、core 派生文字和 section metadata",
        "重新设计整篇",
        "重新出一版",
            "完整 mutable block scope",
        "修改了哪些章节",
        "哪些章节未改变",
        "时长变化",
        "直接读取新的 current child",
        "不得设计“查看新稿/继续当前稿”选择",
        "不得自动确认初稿",
        "撤销刚才的修改",
        "当前 child 中所有将改变或消失的 blocks",
        "上一轮新增的替换 blocks",
        "另一个不可变 child",
        "不实现内嵌聊天",
        "不自动注入选区",
        "不自动替用户粘贴内容",
    ):
        assert_contains(revise, required)

    assert_in_order(
        revise,
        "`project_open`",
        "`content_draft_read`",
        "`workflow_action(submit_draft)`",
        "`content_draft_read`",
        "修改了哪些章节",
    )


def test_relative_first_paragraph_target_is_frozen_and_never_rebound() -> None:
    revise = SKILLS["revise-roughcut"]

    assert_in_order(
        revise,
        "第一段",
        "首次解释相对指代时立即冻结",
        "candidate ID、目标 block IDs/exact refs 和短引文",
        "写入前重新读取精确 current candidate",
        "逐项比较冻结目标",
        "删除、移动或替换",
        "停止并只询问一个确认问题",
        "不得把原命令静默重绑到当前下一段",
    )
