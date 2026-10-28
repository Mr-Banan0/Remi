# V56 Evidence Slot Planner

日期：2026-10-27。V56 保留 V53 Append First 写入和 V55 Objective-Aware Jev 排序，收敛 Search Planner 的职责与输出协议。V55 双模型 Golden 是本次修改前的比较基线；V56 已完成双模型 Golden，结果见 [V56 双模型对照](../golden-v56-model-comparison.md)。

## 职责边界

模型负责三件语义工作：判断查询模式、选择证据角色、编写检索目标。程序负责检索字段默认值、必需角色、日期支持、排序运算、召回充分性、一次协议修正和有界补搜。Jev 继续根据原问题及候选实际命中的检索目标完成最终排序。

Planner 输出收敛为：

```json
{
  "query_mode": "action",
  "action_time_expression": "tomorrow",
  "searches": [
    {
      "role": "constraint",
      "query": "Conditions affecting the user's ability and safety when running",
      "time_expression": null,
      "anchor_source_id": null,
      "anchor_step": null,
      "relation": "independent"
    },
    {
      "role": "latest_state",
      "query": "Later evidence showing whether those limitations still apply",
      "time_expression": null,
      "anchor_source_id": null,
      "anchor_step": null,
      "relation": "independent"
    }
  ],
  "relevant_memory_ids": []
}
```

旧协议要求模型重复填写 purpose、time_basis、select、window_days 等机械字段。V56 删除这条旧路径，由程序编译为内部 RecallStep。代码、测试和 Prompt 统一使用 searches，没有保留双协议兼容层。

## 查询模式与证据槽位

| query_mode | 程序要求的角色 | 查询目标 |
| --- | --- | --- |
| direct | 可选 direct | 直接回答事实；已有证据充分时允许空 searches |
| verify_premise | premise_check | 保留人物、地点和明确日期，检索实际发生的活动 |
| action | constraint、latest_state | 检索完成行动所需能力的限制，再检索限制是否仍然有效 |
| temporal_join | temporal_anchor、answer_detail | 先定位事件，再按同日、之前或之后连接答案 |
| current_state | latest_state，第二项可选 | 检索当前有效状态及解释它的条件或细节 |

缺少必需角色属于协议错误。程序将逐字段错误、原始输出和完整输入交给同一 Planner 修正一次。修正后仍无效时，程序根据已经识别的 query_mode 生成完整的通用计划。供应商不可用时，程序用通用意图规则选择兜底模式。模型明确返回 direct 空计划且直接证据充分时保持空计划。

DeepSeek 使用 JSON object 传输，系统 Prompt 现在包含完整 JSON 模板，并提供前提核验、行动建议和时间连接三个英文 few-shot。GPT 继续使用同一个语义协议及严格 JSON Schema。

## 时间编译

time_expression 必须由原问题支持；action_time_expression 单独保存新行动的时间，不限制历史证据。程序按角色生成 purpose 和 time_basis，按问题中明确的 first／earliest、latest／current 表达生成 select，其余查询使用 all。latest_state 固定读取最新适用状态。anchor_step=0 只允许第二项引用第一项的唯一事件时间。

## 程序补搜

每个角色先执行模型生成的精确查询。程序用两个可审计信号判断召回是否充分：前三项词元覆盖率达到 0.6，或混合 RRF 首项分数达到 `role_recall_min_score`。默认阈值为 0.02；单通道 rank 1 的 RRF 分数约为 1/61，因此该阈值表示更强的跨通道信号。阈值可通过 `REMI_ROLE_RECALL_MIN_SCORE` 配置。

信号不足时，同一角色、同一主体范围和同一时间范围内执行一次通用宽化查询。宽化查询覆盖实际活动、能力限制、后续状态、事件锚点或事件结果。精确通道和宽化通道分别进入逻辑 Memory 级 RRF，并把各自的 role、query 写入 retrieval_objectives。该过程不增加 LLM 调用。

图扩展从角色召回结果产生种子。普通角色候选不获得强制位置；Jev 根据原问题、原始证据和 retrieval_objectives 排序。Jev 分数不完整时保持 RRF 顺序。明确的 first／latest 运算只在 Jev 验证活动后执行。

## 调用与诊断

Search 默认调用 Planner 一次。协议错误时增加一次修正调用；程序补搜、RRF、图扩展和 Jev 衔接不增加 Planner 调用。trace 保存：

- planner_attempts：每次原始输出与逐字段错误；
- decision：经过验证的语义计划；
- compiled_steps：程序生成的内部检索字段；
- tool_results：初始最高分、词元覆盖率、是否宽化、宽化查询及覆盖状态；
- candidate_channels：每个精确、宽化和图通道的角色与来源；
- degradation：decision_invalid、decision_unavailable、供应商或 rerank 降级。

## 验证结果

全套离线测试为 **605 passed、15 skipped**。新增验证覆盖：

- 非法 Planner 字段进入一次带精确错误的修正；
- 连续非法输出按 query_mode 生成完整兜底计划；
- 原始 Planner 输出和两次错误均进入 trace；
- action 固定产生 constraint 和 latest_state；
- verify_premise 和 temporal_join 角色合同及三个 few-shot；
- 弱召回触发一次有界宽化并形成独立 retrieval objective；
- 普通角色不覆盖 Jev 最终排序；
- 时间关系、版本链、服务降级和旧 Search 回归继续通过。

V56 沿用 sources、memories、objects、edges、requests、search_documents 六个逻辑集合，没有创建数据库或新增表。

## 双模型 Golden

2026-10-28 使用相同数据集和空的六逻辑集合完成两轮实验。GPT-4o-mini R@10 为 82.05%、全证据 9/13、任一证据 13/13、MRR 0.8487；DeepSeek Flash 为 79.91%、9/13、12/13、0.7532。两模型都高于 V55 的 R@10 与 MRR。默认 0.02 充分性阈值使 GPT 的 23 个和 DeepSeek 的 24 个角色步骤全部直接通过，程序宽化补搜为 0 次；本轮改善归因于证据槽位与查询协议。完整 Planner、Jev、延迟和 bad case 见 [V56 双模型 Golden](../golden-v56-model-comparison.md)。
