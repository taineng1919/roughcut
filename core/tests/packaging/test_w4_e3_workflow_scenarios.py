from __future__ import annotations

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


def compact(text: str) -> str:
    return "".join(text.split())


def assert_contains(text: str, phrase: str) -> None:
    assert compact(phrase) in compact(text)


def assert_in_order(text: str, *operations: str) -> None:
    text = compact(text)
    position = -1
    for operation in operations:
        operation = compact(operation)
        next_position = text.find(operation, position + 1)
        assert next_position >= 0, f"missing {operation} after {text[position:]!r}"
        position = next_position


def user_layer(content: str) -> str:
    start = content.index("## 面向用户的引导")
    end = content.index("## 仅供 Agent 执行的顺序", start)
    return content[start:end]


def test_w4_e3_candidate_scope_and_cost_authorization_scenario() -> None:
    basics = SKILLS["roughcut-basics"]

    for required in (
        "候选素材清单",
        "不递归扫描",
        "候选不会自动导入、转录或授权",
        "建议",
        "自动识别事实",
        "增删、替换和改名",
        "精确素材清单",
        "不删除文件或历史记录",
        "稳定原始目录",
        "宿主临时或缓存附件",
        "每次只解释当前动作、精确素材范围和可见成本",
        "明确授权",
    ):
        assert_contains(basics, required)


def test_w4_e3_basics_restores_safe_tool_execution_and_readback_order() -> None:
    basics = SKILLS["roughcut-basics"]

    assert_in_order(
        basics,
        "`health`",
        "`project_open`",
        "`source_add`",
        "`source_metadata_update`",
        "`proxy_read`",
        "`proxy_create`",
        "`transcript_versions_read`",
        "`transcribe_source`",
        "`people_read`",
        "`speaker_map_confirm`",
    )
    for required in (
        "用户确认精确素材清单和 `output_preset` 后",
        "每次写入后调用 `people_read` 和 `project_open` readback",
        "每个高成本动作前都重新 `project_open`",
        "返回 stale、revision conflict 或其他失败时，立即进入上面的只读诊断阶段",
        "诊断期间不得重放、合并、覆盖",
    ):
        assert_contains(basics, required)


def test_w4_e3_reuses_existing_results_without_running_them_again() -> None:
    basics = SKILLS["roughcut-basics"]

    for required in (
        "已有代理、转录或人物对应时",
        "直接复用",
        "先尝试原素材播放",
        "不因 4K 或 1080p 自动生成代理",
        "代理、转录、说话人处理、组件下载和正式导出",
    ):
        assert_contains(basics, required)


def test_w4_e3_create_skill_covers_every_transcript_page_but_not_user_pages() -> None:
    create = SKILLS["create-roughcut"]
    readable = create

    assert_contains(readable, "Agent 必须覆盖完整 Readable Transcript，但不要求用户逐页通读")
    assert_contains(readable, "不要求用户逐页通读原始转录稿")
    assert_in_order(
        create,
        "`workflow_status`",
        "`transcript_versions_read`",
        "`readable_transcript_read`",
        "`transcript_selection_resolve`",
        "`workflow_action(confirm_brief)`",
        "`agent_context`",
        "`multi_source_context`",
        "`workflow_action(submit_outline)`",
        "`workflow_action(approve_outline)`",
        "`workflow_action(submit_draft)`",
        "`content_draft_read`",
        "`workflow_action(approve_draft)`",
        "`workflow_action(adopt_roughcut)`",
    )
    for required in (
        "直到没有下一页",
        "每页必须保持同一 revision、bindings 与 view hash",
        "每页保持同一 revision、bindings 和 `context_hash`",
        "未录解说时停止",
        "同时生成 confirmed child 和待审阅粗剪候选",
        "不得由 Agent 或浏览器重建 clips",
        "stale 时，停止并重新读取",
    ):
        assert_contains(create, required)


def test_w4_e3_create_skill_keeps_brief_and_initial_draft_as_distinct_user_gates() -> None:
    create = SKILLS["create-roughcut"]

    for required in (
        "主题：请 Agent 根据完整文稿推荐 / 用户填写", "具体内容主题", "作品类型", "不能互相替代",
        "1–3 个主题建议", "用户最终确认的具体主题", "Brief theme", "成片类型：", "人物专访", "新闻专题", "纪录短片", "活动回顾", "新媒体短视频",
        "表达风格：", "信息密度高", "建议保留：", "建议排除：", "目标时长：X 分 X 秒",
        "解说：无解说，以同期声为主 / 需要解说 / 请 Agent 推荐",
        "内容顺序：允许按主题重组 / 尽量保持原顺序 / 请 Agent 推荐", "其他要求：",
        "只追问真正影响结果的信息",
        "初稿确认只确认内容与顺序，不等于采用粗剪或批准正式导出",
        "生成粗剪预览",
        "采用这个粗剪版本",
        "同期声原话不得改写、漏词或补写",
    ):
        assert_contains(create, required)


def test_w4_e3_multicam_main_scope_independent_of_diarization() -> None:
    basics = SKILLS["roughcut-basics"]
    create = SKILLS["create-roughcut"]

    for required in (
        "ASR source scope 与 speaker diarization 是两个独立维度",
        "绝不能增加、替换或扩大 source scope",
        "多机位先确定主收声 scope，再只对这个 scope 应用 diarization",
        "不得逐素材重复询问",
        "只对已选主收声 Source 生效",
        "说话人区分只是主收声 Source 的参数",
        "不会把副机位加入 ASR 范围",
    ):
        assert_contains(basics + create, required)
    # Ensure the 4+4 scenario: 4 main + 4 aux with diarization true
    # still only 4 in authorizations
    assert_contains(basics, "只有用户确认的主收声/内容主线机位进入本轮内容整理和 ASR")
    assert_contains(basics, "副机位虽已导入 Project")
    assert_contains(basics, "不得加入 `source_authorizations`")
    assert_contains(basics, "多机位副机位不在此循环中，不调用")


def test_w4_e3_multicam_project_media_and_asr_scope_are_independent() -> None:
    basics = SKILLS["roughcut-basics"]
    create = SKILLS["create-roughcut"]

    for required in (
        "进入当前 Project 的素材集合”和“进入 ASR/content scope 的 Source 集合”是两个独立集合",
        "用户确认进入当前 Project 的全部媒体都必须先导入 Project",
        "`source_add` 必须覆盖用户确认进入当前 Project 的全部媒体",
        "`source_authorizations`/`transcribe`",
        "只能决定已经导入的 Source 中哪些进入内容/ASR",
        "绝不能反过来过滤 `source_add`",
        "4 条主轨 + 4 条副轨 = 8 条项目素材",
        "confirmed media = 8",
        "Project Sources = 8",
        "ASR/content Sources = 4 main",
        "aux Sources = 4",
        "readback 必须确认",
        "`Project Sources = 8` 后才允许 `workflow_start`",
        "`source_add × 8`",
        "`transcribe × 4 main only`",
        "`aux × 4` remain in Project, no ASR",
        "副机位虽已导入 Project",
        "不进入 Brief、Outline、Draft 或任何内容决定",
        "不需要副机位 Transcript",
        "不新增 Core gate",
    ):
        assert_contains(basics, required)

    assert_in_order(
        basics,
        "已确认 Project media",
        "全部 `source_add`/精确 Source 复用完成",
        "`project_open`",
        "核对本轮确认媒体的存在性与 exact match",
        "才允许 `workflow_start`",
    )
    for required in (
        "进入当前 Project 的素材集合和进入 ASR/content scope 的 Source 集合是两个独立集合",
        "用户确认进入 Project 的全部媒体必须先导入 Project",
        "不能反过来过滤 Project import",
        "声音对轨直接使用原 Source 音轨",
        "不要求副机位 Transcript",
    ):
        assert_contains(create, required)


def test_w4_e3_existing_project_allows_historical_sources_without_scope_leak() -> None:
    basics = SKILLS["roughcut-basics"]

    for required in (
        "新 Project 必须把本次确认进入 Project 的全部媒体逐一导入",
        "一条不能漏",
        "已有 Project 只要求本轮确认媒体全部存在且 exact match",
        "允许保留已有 Project 中不参与本轮任务的历史/额外 Source",
        "历史/额外 Source 不得自动进入当前 ASR/content scope 或机位授权",
        "不要求 `project.sources` 全局顺序与本轮确认清单完全相同",
        "只核对本轮媒体的 membership 与 exact match",
        "当前 scope 顺序只来自本轮确认",
    ):
        assert_contains(basics, required)

    assert_in_order(
        basics,
        "已有 Project 只逐一核对本轮确认媒体是否已有且 exact match",
        "允许保留已有 Project 中不参与本轮任务的历史/额外 Source",
        "不要求 `project.sources` 全局顺序与本轮确认清单完全相同",
        "历史/额外 Source 不得自动进入当前 ASR/content scope",
    )


def test_w4_e3_failed_submit_draft_allows_only_read_only_diagnosis() -> None:
    basics = SKILLS["roughcut-basics"]
    create = SKILLS["create-roughcut"]
    combined = basics + create

    for required in (
        "任何 state-changing workflow action 失败后",
        "不是 validator",
        "严格禁止新的 state-changing action、换 `action_id`、缩 payload",
        "构造 dummy/minimal/single-block candidate",
        "通过“先写进去看看”探测",
        "直接修改 Project JSON、改变业务对象",
        "立即进入“只读诊断阶段”",
        "根因未确定前",
        "严格禁止新的 state-changing action",
        "后续动作仅限",
        "`workflow_status`、`project_open`、现有 read-only 工具",
        "保存并分析 exact error",
        "只读证据确定根因后",
        "业务意图不变的安全机械纠错",
        "一次正式恢复动作",
        "涉及新的用户决定",
        "说明根因与影响并取得明确决定",
        "恢复动作是正式执行，不得继续被当 validator",
        "若只读证据不能证明安全则停止并报告",
        "只读重算 hash",
        "重新读取 current status",
        "正确 offset 继续分页",
        "保留现有合法 scope reapproval 语义",
        "不增加通用 preflight API",
        "“失败计数”状态",
        "不扩 Core hard gate",
    ):
        assert_contains(combined, required)

    assert_in_order(
        create,
        "`workflow_action(submit_draft)`",
        "state-changing workflow action",
        "立即进入“只读诊断阶段”",
        "根因未确定前",
        "严格禁止新的 state-changing action",
        "后续动作仅限",
        "`workflow_status`",
        "只读证据确定根因后",
        "一次正式恢复动作",
        "恢复动作是正式执行，不得继续被当 validator",
    )
    assert "不得调用新的 state-changing action" not in combined


def test_w4_e3_missing_multicam_prerequisites_never_auto_select_main_only() -> None:
    render = SKILLS["render-roughcut"]

    for required in (
        "当前任务原本已由用户确认是多机位交付",
        "副机位 Source、机位授权或 alignment prerequisite 缺失",
        "前序多机位准备不完整",
        "停止多机位导出准备",
        "不得把缺失静默解释为“没有副轨”",
        "不得静默把原来的多机位任务降级成 B「仅主 MP4」",
        "只有在用户得知缺失及影响后明确决定“这次只导出主片”",
        "才能进入 B 路径",
        "B 不是副机位前置条件缺失时的自动 fallback",
        "不得落入 B 路径或把原任务改写成仅主 MP4",
    ):
        assert_contains(render, required)

    assert_in_order(
        render,
        "副机位 Source、机位授权或 alignment prerequisite 缺失",
        "前序多机位准备不完整",
        "停止",
        "不得落入 B 路径",
        "明确决定“这次只导出主片”",
        "按 B 执行",
    )


def test_w4_e3_auxiliary_alignment_does_not_require_a_transcript() -> None:
    combined = "\n".join(
        SKILLS[name] for name in ("roughcut-basics", "create-roughcut", "render-roughcut")
    )

    for required in (
        "副机位声音对轨直接读取原 Source 音轨",
        "不需要副机位 Transcript",
        "不要求副机位 Transcript",
        "副机位 alignment 只依赖已导入的原 Source 音轨及合法机位授权",
    ):
        assert_contains(combined, required)
    for forbidden in (
        "auxiliary transcript required for alignment",
        "auxiliary transcript is required for alignment",
        "副机位必须转录才能对轨",
        "副机位 Transcript 是对轨前置条件",
        "副机位 Transcript 是 alignment prerequisite",
    ):
        assert forbidden not in combined


def test_speaker_waiver_requires_explicit_user_choice() -> None:
    create = SKILLS["create-roughcut"]
    assert_contains(create, "非空但用户尚未明确决定")
    assert_contains(create, "用户明确选择不 waive 时继续 speaker mapping")
    assert_contains(create, "eligible 仍非空期间同样不得调用 `confirm_brief`")
    assert_contains(create, "已为空时，调用 `confirm_brief` 才传空数组")


def test_gate_blocked_is_diagnostic_event_never_silent_mutation() -> None:
    basics = SKILLS["roughcut-basics"]
    assert_contains(basics, "首先是诊断事件")
    assert_contains(basics, "数据未就绪、Agent 执行错误、Core 缺陷、需用户决策")
    assert_contains(basics, "静默改变已确认业务事实")
    assert_contains(basics, "先向用户报告")


def test_pagination_and_review_readiness() -> None:
    create = SKILLS["create-roughcut"]
    assert_contains(create, "直到没有下一页")
    assert_contains(create, "只要 `next_offset` 非空就不得")
    assert_contains(create, "禁止直接读取 `transcripts/*.json`")
    assert_contains(create, "必须同时满足同一进程仍存活")
    assert_contains(create, "不得仅凭 startup JSON/URL 报告")


def test_w4_e3_trial_readiness_material_speaker_and_transcript_contract() -> None:
    basics = SKILLS["roughcut-basics"]
    roughcut = SKILLS["roughcut"]

    for required in (
        "A_主收声", "B_副机位", "C_副机位", "A 机（主收声/内容主线）",
        "内容主线/主收声机位：A / B / C / 待确认", "主收声全程稳定：是 / 否 / 不确定",
        "不能仅凭 ffprobe、文件名、音轨数量或时长断言主收声", "ffprobe 只报告是否有音轨和技术参数",
        "只有用户确认后才成为事实", "现有一次素材确认摘要", "不新增确认门",
        "不要求文件数量一一对应", "不要求逐文件配对", "取得路径后", "磁盘估算", "预计运行时间",
        "不重命名、移动或覆盖媒体", "同名 XML、字幕或其他 sidecar",
        "首次 ASR 前一次决定", "speaker_diarization=true", "2–3 条代表性原话",
        "用户提供，原声未出现姓名", "transcript_correct", "创建并自动激活不可变 Transcript 子版本",
        "不需要再调用", "transcript_version_activate", "这不会重跑识别，也不会改变时码",
        "不修改 ticks、segment identity、original_text、fine units 或 speaker evidence",
    ):
        assert_contains(basics, required)
    assert_in_order(
        basics,
        "transcript_correct",
        "创建并自动激活不可变 Transcript 子版本",
        "不需要再调用",
        "`transcript_version_activate`",
        "不重跑 ASR",
    )
    for required in (
        "所有提问、进度、摘要、确认、错误和交付报告使用中文",
        "复制诊断信息",
        "内容状态已更新，请重新打开当前版本",
    ):
        assert_contains(roughcut, required)


def test_w4_e3_trial_readiness_draft_outline_and_delivery_contract() -> None:
    create = SKILLS["create-roughcut"]
    render = SKILLS["render-roughcut"]

    for required in (
        "默认范围为目标时长至目标时长 +10%", "宽松范围至 +20%", "不得生成短于目标的粗剪", "报告缺口",
        "每章“目标时长”", "修改会创建新版本，不会覆盖历史版本",
        "同一 Source/Transcript 上按上下文顺序连续", "跳过一个真实 Transcript segment", "不伪造中间 ref",
        "`section_title` 与 `source_excerpt` 是独立 block", "seg_1、seg_2、seg_3", "两个 `source_excerpt` blocks",
    ):
        assert_contains(create, required)
    a_tail = render.split("### A. 主 MP4 + 副机位平行 MP4", maxsplit=1)[1]
    a, b_tail = a_tail.split("### B. 仅主 MP4", maxsplit=1)
    b, c_tail = b_tail.split("### C. 仅副机位参考文件", maxsplit=1)
    c, _common = c_tail.split("### 通用正式导出安全约束", maxsplit=1)

    assert_in_order(
        a,
        "调用一次 `multicam_parallel_render_prepare`",
        "prepare 摘要与主 MP4 exact export 摘要合并展示",
        "等待一次组合批准",
        "调用一次 `multicam_parallel_render_start`",
        "`media_operation_status` readback",
        "重新读取 `workflow_status`",
        "直接调用 `workflow_action(approve_export)`",
    )
    assert a.count("`multicam_parallel_render_prepare`") == 1
    assert a.index("在批准前调用一次 `multicam_parallel_render_prepare`") < a.index("#### 批准后")
    assert "组合路径不得再次询问主 MP4 导出批准" in a
    for required in (
        "同一 Decision/export subject 仍 current", "subject 已变化或 stale", "停止并要求重新确认",
        "副机位 operation 无论成功或失败都不自动重试", "分别报告主片和副机位的真实结果",
    ):
        assert_contains(a, required)

    for forbidden in ("multicam_parallel_render_prepare", "multicam_parallel_render_start", "prepare_ref"):
        assert forbidden not in b
    assert "workflow_action(approve_export)" in b
    for required in ("展示主 MP4 exact export 摘要", "等待一次独立批准", "做正式导出 readback"):
        assert_contains(b, required)

    for required in (
        "调用一次 `multicam_parallel_render_prepare`", "调用一次 `multicam_parallel_render_start`",
        "同一 operation ID 做一次 `media_operation_status` readback",
    ):
        assert_contains(c, required)
    assert "approve_export" not in c


def test_create_skill_keeps_review_service_alive_until_user_finishes() -> None:
    create = SKILLS["create-roughcut"]

    assert_in_order(
        create,
        "`workflow_action(submit_draft)`",
        "`content_draft_read`",
        "`roughcut review`",
        "`/api/workflow/draft-editor`",
        "`workflow_action(approve_draft)`",
    )
    for required in (
        "保存进程或终端 session 句柄",
        "不能在交付 URL 后结束该执行单元",
        "同一进程仍存活",
        "用户明确完成、取消或要求停止后才关闭进程",
        "只使用同一 bindings 和不可变 draft 重新启动",
        "不得重建初稿、重跑转录或把旧 URL 继续交给用户",
    ):
        assert_contains(create, required)


def test_w4_e3_revise_skill_restores_direct_and_semantic_workflows() -> None:
    revise = SKILLS["revise-roughcut"]

    assert_in_order(
        revise,
        "`project_open`",
        "`content_draft_read`",
        "冻结 candidate",
        "`workflow_action(submit_draft)`",
        "不可变 child",
        "写入后调用 `content_draft_read`",
        "从粗剪返回初稿",
        "`workflow_action(return_to_draft)`",
        "`workflow_action(approve_draft)`",
        "重新选择“采用这个粗剪版本”",
    )
    for required in (
        "mutable block IDs",
        "未指定 blocks",
        "旧 Proposal 和 Decision 保持字节不变",
        "不创建 child、不确认初稿、不增加 revision",
        "`edit_change`、`edit_undo`、`edit_redo` 与 history/schema 继续兼容旧调用",
        "不得用它们绕过返回初稿和重新确认",
    ):
        assert required in revise


def test_w4_e3_revise_skill_keeps_preview_and_stale_behavior_separate() -> None:
    revise = SKILLS["revise-roughcut"]

    for required in (
        "Agent 的初稿局部改稿",
        "初稿页面机械调整",
        "粗剪预览与采用",
        "不得用 Edit 工具绕过初稿确认",
        "不在粗剪页重复",
        "返回初稿并修改后，旧粗剪失效",
        "页面或项目 stale 时保留内容供查看但停止写入",
    ):
        assert_contains(revise, required)


def test_w4_e3_render_skill_requires_current_decision_and_explicit_export() -> None:
    render = SKILLS["render-roughcut"]
    readable = render

    assert_in_order(
        render,
        "`workflow_status`",
        "`project_open`",
        "`decision_read`",
        "`workflow_action(approve_export)`",
        "MP4、manifest 和 acceptance checks",
    )
    for required in (
        "当前片段数、总时长、分辨率、帧率、音频、输出位置、是否读取原素材和预计执行动作",
        "语义等价的明确表达批准正式导出当前版本",
        "输出画面、帧率和音频设置从当前 Project settings 读取",
        "从它读取 clips、时长和原素材读取范围",
        "不得调用低层 `render_roughcut`",
        "直接调用 FFmpeg、替换 Decision，或把代理当作正式输入",
        "stale 或不一致都停止",
    ):
        assert_contains(readable, required)


def test_w4_e3_user_navigation_does_not_expose_internal_object_names() -> None:
    for name in ("roughcut-basics", "create-roughcut", "revise-roughcut", "render-roughcut"):
        content = SKILLS[name]
        layer = user_layer(content)
        for internal_name in ("Brief", "Proposal", "Decision", "Content Draft"):
            assert internal_name not in layer


def test_w4_e3_ambiguous_continue_cannot_cross_confirmation_gates() -> None:
    combined = "\n".join(SKILLS.values())

    assert_contains(combined, "“继续”“可以”“往下做”等模糊回复不能跨越多个确认门")
    assert_contains(combined, "初稿、采用粗剪或正式导出")
