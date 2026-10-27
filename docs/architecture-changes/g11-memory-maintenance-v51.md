# V51：单一决定位置的记忆维护闭环

日期：2026-10-26。实验 pipeline：`G11-memory-maintenance-v51`。

## 目标与边界

V51 保留“主体维护 → 候选记忆提取 → 主体范围内召回 → 记忆维护”的三次模型调用顺序。主体阶段继续维护稳定 ID、本名、别名和 person／animal／organization 类型。本轮冻结主体逻辑，只收敛记忆维护协议和检索供应商的瞬时错误处理。

系统沿用 sources、memories、objects、edges、requests、search_documents 六个逻辑集合和 schema_version=13。本轮没有创建数据库表。

## 三个阶段的职责

| 阶段 | 模型决定 | 程序确定 |
| --- | --- | --- |
| 主体维护 | 当前原文中的主体是创建、复用还是待定；有效名字和别名 | 稳定主体 ID、来源校验、主体 scope |
| 候选提取 | fact／preference／experience／event、参与主体、表达归属、条件、事件状态、日期表达和来源 | C 引用、来源原文复制 |
| 记忆维护 | create／update／pending、update 目标、完整最终正文 | 类型、参与者、表达归属、条件、状态、时间、证据和修订差异 |

记忆维护模型只返回五个字段：`ref`、`operation`、`target_id`、`content`、`pending_reason`。相同语义决定只保留一个表达位置。

## 召回与操作

主体确认后，程序先在该主体 scope 内召回记忆。主体通道相关性不足时，补充无主体记忆。只属于其他主体的记忆保持在 scope 外。每个候选会显示同类型的可选 M 目标。

操作定义：

- create：独立的新记忆，`target_id=null`。
- update：延续、更正、完成或支持一条同类型的已召回记忆，模型返回完整当前正文。
- pending：当前表达明确依赖旧记忆，但供应的候选不足以安全定位。

程序比较 update 的最终视图和旧视图。有字段变化时生成 update 修订；视图一致时生成 support 修订并追加来源。每个提取候选都必须获得 created、updated、supported、pending 或 validation_failed 处置。模型漏掉的候选记录为 `candidate_omitted_by_model`。

## 程序化字段

create 使用候选的主体集合。update 保留旧主体并加入当前候选主体。参与者移除退出当前模型协议；事件历史参与者继续作为该事件的图连接。

事实、偏好和经验的时间范围保留在正文。事件日期由候选的 status 和 time_expression 决定：planned／desired 写入 planned_at，occurred 写入 event_at。update 保留未被本轮候选更新的旧日期，因此完成事件可以同时保留原计划日期和实际发生日期。来源证据由候选复制，并在 update／support 中与旧证据合并。

同轮事件连边及连边增删退出本轮模型协议。当前维护模型专注于单条记忆身份和最终视图；既有历史连接由修订回放保留。

## 重试与校验

模型输出使用严格 JSON Schema。程序校验候选 C 引用唯一、update 目标已召回且类型一致、一个 M 目标只更新一次、pending 原因完整，以及正文中的绝对日期有来源支持。部分有效项在修正期间保持受保护。

Jev 每个批次对连接错误、超时、429 和 5xx 进行一次有界重试。400 等配置错误不重试。第二次仍失败时保留已有基础排序和其他成功批次。

## 完成标准

- few-shot 回复全部符合线上五字段 Schema，并通过实际校验。
- 每个提取候选都有处置结果。
- 同一事件的计划、改期和完成保持同一记忆 ID，形成 create → update → update 修订。
- 完成事件保留 planned_at，并写入 event_at。
- 相同事实的变化更新同一记忆 ID。
- 离线全量测试通过，真实 GPT 生命周期专项通过后再运行 Golden。

## 冻结结果

V51 离线 596 项通过，真实事件专项 3/3 通过。完整 GPT Golden 的 R@10 为 69.23%，全证据 8/13、任一证据 10/13、MRR 0.7308；Add／Search 均时为 75.11／15.16 秒。

主集形成 97 条记忆、213 个 revision，其中 update 113 次、support 3 次，最长 revision 链为 17。实际审计发现独立事件和认识被合并进同一记忆卡，生命周期第四步“另一次搬家”也覆盖进已完成事件。该结果促成 V52 候选级身份门。完整对照见 [V51／V52 Golden](../golden-v51-v52-results.md)。
