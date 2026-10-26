# V36 主体修正：字段反馈、正确部分保护与真实验证

日期：2026-10-22。完整离线 **407 passed、0 skipped**，13.21 秒。两组真实模型使用最终冻结代码及相同主体输入；本轮仅验证主体，Golden 保留 V35 结果，生产入口继续为 V32。

## 已实现

修正调用收到原始任务、上一轮 assistant JSON、主体 ref、字段路径、实际值、预期约束和相关原文编号。同一主体的来源与提及等相关错误一次报告。输出合同沿用 subjects／connections。

程序保留已经通过校验的主体、有效名字／描述以及连接。完整修正响应可以撤回尚未确认且当前原文不支持的主体，并清理依赖它的待定连接。原始修正、保护后的实际输出和保护动作分别记录。首错字符串反馈及缺少上一轮输出的修正路径已经退出；现有诊断字段作为统一 issues 的投影保留，供报告读取。

每个文本单元正常两次 Add 模型调用，主体与四类记忆共享至多一次协议修正。修正请求失败时复用已保存的初次输出和相同修正请求。来源、用户归属、时间可见性和身份依据校验继续执行。

完整步骤及修改边界见 [V36 架构](architecture-changes/g11-structured-repair-v36.md)。

## 同一批主体专项

| 模型 | 28组单轮 V35→V36 | 扩展10组 V35→V36 | 原4组跨轮 V35→V36 | 描述4组跨轮 V35→V36 |
| --- | --- | --- | --- | --- |
| deepseek-flash | 25/28 → 27/28 | 9/10 → 9/10 | 4/4 → 4/4 | 3/4 → 4/4 |
| gpt-4o-mini | 25/28 → 24/28 | 9/10 → 9/10 | 1/4 → 4/4 | 0/4 → 1/4 |

28＋10组单轮沿用一次首次判断的口径，跨轮采用一次修正预算。重新生成的首答也会波动；本表用于相同场景的整条链路回归。跨轮通过率变化同时包含首答与修正的影响，下表固定原始错误和历史上下文，单独观察一次修正。

## 固定旧错误的真实修正重放

从 V35 保存的连续测试逐步重建原有存储，逐表核对与历史记录完全一致。选择当时首次输出存在校验问题的步骤，保持该首次响应与上下文固定，仅向同一模型请求一次修正。对照使用 V35 已保存的最终响应，经同一校验口径重算；该表是历史响应对照，供应商生成仍有波动。

| 模型 | 旧错步骤 | V35最终无校验问题 | V36模型原始修正无校验问题 | V36保护后无校验问题 | 已确认身份退化数：原始／保护后 |
| --- | --- | --- | --- | --- | --- |
| deepseek-flash | 1 | 0/1 | 1/1 | 1/1 | 0 / 0 |
| gpt-4o-mini | 6 | 0/6 | 3/6 | 4/6 | 0 / 0 |

“无校验问题”表示协议与证据约束通过。身份含义是否正确仍由主体专项的预期组别检查。pending 与撤回未确认提议分别保留其含义；这些操作不会自动计为成功绑定身份。模型自行修正与程序保护分开统计，可以追踪效果来源。

- deepseek-flash：首次问题 2 项，一次修正后新增通过身份校验的原失败 ref 为 0 个；程序保护动作 {}。
- gpt-4o-mini：首次问题 12 项，一次修正后新增通过身份校验的原失败 ref 为 4 个；程序保护动作 {"repair_withdrew_unconfirmed_subject": 1, "repair_removed_withdrawn_endpoint_connection": 1, "repair_restored_protected_connections": 1}。

## 诊断阶段发现并修复的程序缺口

第一轮冻结保存在 `subjects-v36-feedback-20261022-v1/`。重放发现，程序把模型主动撤回的失败组织行补回，导致引用当前不存在名称的错误继续存在。最终版本允许完整修正响应撤回未确认主体，并移除其待定连接；已通过身份校验的行及有效增量继续受保护。补充两项回归后完整离线407项通过，再重新冻结并完整运行两模型。

这是通用的提议撤回规则。运行时 few-shot 与主体边界保持原样，规则只读取当前 API 原文、候选、模型提议和校验结果；评测名字、期望值和旧错误留在评测侧。

## 最终仍失败的专项

### deepseek-flash

- `direct/shared-description-people`：0_identity_and_names, 0_description_field_coverage, 1_identity_and_names, 1_description_field_coverage, groups_remain_distinct。
- `holdout/holdout-outside-subject-scope`：ambiguous_subjects_stay_unresolved, expected_subject_rows。

### gpt-4o-mini

- `direct/same-name-animals-zh`：0_identity_and_names, 0_description_field_coverage, 1_identity_and_names, 1_description_field_coverage, groups_remain_distinct。
- `direct/shared-description-people`：0_identity_and_names, 0_description_field_coverage, 1_identity_and_names, 1_description_field_coverage, groups_remain_distinct。
- `direct/different-organization-en`：0_identity_and_names, groups_remain_distinct。
- `direct/changed-role-zh`：0_identity_and_names, 0_description_field_coverage, groups_remain_distinct。
- `holdout/holdout-outside-subject-scope`：ambiguous_subjects_stay_unresolved, expected_subject_rows。
- `extra/shared_description`：description_selects_original_identity。
- `extra/cross_species_namesake`：description_selects_original_identity。
- `extra/changing_role`：1_program_checks, 1_role_history, 2_program_checks, 2_role_history。
  第2轮剩余校验问题：reference_mention_requires_source_text。
  第3轮剩余校验问题：reference_mention_requires_source_text。


本轮失败需要按实际输出区分：

- 两位志愿者在两模型中都建立了两个不同 ID，失分来自 volunteer 描述缺失；该项的整组匹配检查同时依赖描述覆盖。
- GPT 的两只同名猫也建立了不同 ID，但描述使用整句，未通过测试要求的短标签覆盖；宋言沿用正确 ID 并保存阿言，仍漏写画家描述。
- GPT 把明确独立成立的同名组织复用到旧 ID，属于实际身份误归并。两模型都把 router 建成个体，主体边界仍有错误。
- GPT 描述跨轮仍有护士身份错误待定、同名人狗中额外绑定人的身份，以及角色描述遗漏和历史组织被继续带入当前提及等问题。

固定旧错误重放还剩两条：`changing_role-2` 在当前只有“Seli sent a message.”时继续引用历史组织；`shared_description-1` 改成 pending 后仍填写当前原文没有的完整旧称呼，程序继续拒绝。这些结果保留为剩余失败。

每项原文、初次输出、修正原始输出、有效输出、完整请求和逐项检查均保存在对应 case JSON。引用协议通过后的同名消歧、主体边界、描述覆盖和遗漏继续按语义用例分别判断，结构一致性通过与语义全对分别报告。

## 调用、配置与审计

| 模型 | 专项物理文本请求 | 固定错误修正请求 | 专项供应商错误 |
| --- | --- | --- | --- |
| deepseek-flash | 55 | 1 | {} |
| gpt-4o-mini | 59 | 6 | {} |

DeepSeek 使用官方 deepseek-flash、关闭 thinking、JSON object；GPT 使用既有中转站 gpt-4o-mini 和 JSON Schema。任务、few-shot、程序校验与评测输入一致，供应商输出约束差别单列。普通记忆模型、向量和 Jev 的实际请求均为0；真实实验仅使用 MemoryStore。

- 冻结运行代码指纹：`684e0cf9c6fcf53ec0d6d2072ed3f91bb246e3559d557e74ad67ce2bf0c63c15`。
- 审计：`{"complete_both": true, "offline_passed": 407, "frozen_files_unchanged": true, "live_matches_frozen": true, "same_direct_inputs": true, "same_replay_store": true, "protected_identity_regressions": {"deepseek-flash": 0, "gpt-4o-mini": 0}, "model_calls_in_memory_stage": 0, "new_tables_created": 0, "golden_rerun": false, "git_commit": false}`。
- 资料：`.local-data/subjects-v36-feedback-20261022-v2`，包含冻结 runtime、inputs、manifest、两模型全部专项和重放结果、comparison.json 与 failure-review.json。
- 私密凭据仅用于本轮授权模型请求，临时文件在结束后删除；既有 .env 保持原样。离线 SQLite 沿用获准六表，在临时目录验证。Git 未 commit。
