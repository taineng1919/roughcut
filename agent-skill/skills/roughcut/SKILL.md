---
name: roughcut
description: Start or continue a roughcut workflow. Use when the user says “开始粗剪”, “创建粗剪项目”, “继续初稿”, “调整粗剪”, or asks to prepare a formal roughcut export.
---

# Roughcut

Use this as the single roughcut entry when a user says “开始粗剪”、“创建粗剪项目”、
“继续初稿”、“调整粗剪”或“导出粗剪”。A host may select this Skill or a user may
explicitly invoke `$roughcut`; `/roughcut` is only a 宿主可选快捷别名, not a cross-Agent
standard.

`/roughcut` or `$roughcut` selects this Skill; it is never a shell command. If the host exposes
the Roughcut stdio MCP, use its native tools for routing and quick checks. Do not search PATH,
inspect a source checkout, or locate a bare `roughcut` executable. A CLI fallback is allowed only
when MCP is unavailable and installation has already supplied an absolute CLI path.

This is a router, not a second workflow. Do not duplicate the business rules in the Skills
below, edit project files, or infer user approval from a vague continuation. For an existing
Project, call `workflow_status` before routing and use only its current user-facing task and
allowed action. Start or cancel a run only through `workflow_start` or `workflow_cancel`.
If the user returns with an already-held Project-media operation ID after a Host task/session
disappeared, call `media_operation_status` once before routing; do not sleep, poll the missing
Host task, scan artifacts, or infer success.
Keep stage, hash, receipt, basis, ref, and action names internal; use only 素材、剪辑要求、
大纲、初稿、粗剪预览和正式导出 in user-facing guidance.

全局用户可见规则：所有提问、进度、摘要、确认、错误和交付报告使用中文。“剪辑要求、提纲、
初稿、粗剪候选、已采用的粗剪、正式导出”分别是 Brief、Outline、Content Draft、Proposal、
Decision、Render 的用户名称；`submitted/approved` 说成“已准备好，等待你确认”或“已确认”，
`stale` 说成“内容状态已更新，请重新打开当前版本”。action、schema、ref、hash、receipt 和
candidate ID 只放进“复制诊断信息”，不作为普通导航或确认话术。

1. New project or user-confirmed material scope: route to `roughcut-basics`, then
   `create-roughcut` after preparation.
2. Existing initial draft or a request to continue drafting: route to `create-roughcut`.
3. A request to adjust an initial draft or roughcut preview: route to `revise-roughcut`.
4. A request to formally export an already adopted roughcut: route to `render-roughcut`. MP4 and editable NLE handoff are separate actions; NLE must use `approve_nle_export` and never be inferred from `approve_export`.

For a user request involving auxiliary-camera grouping or parallel camera output, keep this
router as the only entry point: route grouping and authorization collection to `create-roughcut`,
and route the later coverage review and explicitly approved parallel output to `render-roughcut`.
Do not start alignment or parallel rendering from the router.

Whenever routing through `create-roughcut`, the outline gate after confirmed editing
requirements and before the initial draft is mandatory. Even for a 单素材 project or when
the Agent thinks the 顺序明确, it 不得跳过大纲.

Read `references/tool-contract.md` before making local machine calls. If the request could
mean more than one route, use `📌 需要你确认` and ask which user-facing task they want to do.
