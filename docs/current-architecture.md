# Remi 0.2.0 当前架构

当前唯一运行入口是 `remi.engine.MemoryEngine`。FastAPI、CLI、AML Runner 和测试共同使用该入口，pipeline 为 `remi-0.2.0`，持久记录为 `schema_version=13`。

## Add

```text
API messages
  → 保存完整 Source
  → 关键词召回主体候选
  → LLM 维护主体身份
  → 程序校验并固定主体 scope
  → LLM 提取四类记忆候选
  → scope 内召回旧 Memory
  → 相关性不足时补充无主体 Memory
  → LLM 选择可选目标并定稿正文
  → 程序计算差异和版本边
  → 原子保存主体、Memory、图、索引和检查点
```

| 阶段 | 输入 | 输出 | 模型调用 |
|---|---|---|---:|
| Source 保存 | API 消息、用户、会话、消息时间 | 完整原文和稳定 Source ID | 0 |
| 主体召回 | 当前原文、最近 1 条同会话上下文、现有名称和别名 | 少量主体候选及身份依据 | 0 |
| 主体维护 | 当前原文、上下文、候选主体 | create／reuse／pending | 1 |
| 主体校验 | 模型输出、候选证据、同名集合 | 固定主体 ID 和本轮 P 句柄 | 0 |
| 候选提取 | 当前原文、上下文、已确认主体 | fact／preference／experience／event 候选 | 1 |
| Memory 召回 | 候选正文、固定主体 scope、历史 Memory | 每个候选最多 3 个同类目标 | 0 |
| Memory 定稿 | 候选及其目标 | ref、可选 target_id、完整正文 | 1 |
| 应用 | 已验证决定、当前快照 | Revision、Edge、索引和处置结果 | 0 |

每个有记忆候选的用户消息正常使用三次文本模型调用。主体和候选提取各允许一次协议修正。Memory 决定的 `target_id` 有效时追加到同一 Memory；空目标和越界目标使用候选正文独立创建。该收敛方式让模型只表达一次“写什么”和“维护哪条”，程序负责计算变化字段。

主体 scope 在主体阶段完成后保持固定。主召回读取与已确认主体相连的 Memory；当主召回相关性不足时，程序在无主体 Memory 中补搜并统一排序。只绑定其他主体的 Memory 不进入本轮维护范围。一条同时包含当前主体和其他人的历史 Memory 可以进入候选。

## 主体

主体只表示可长期保持身份的人、动物和组织：

```text
Subject {
  id
  kind: person | animal | organization
  name
  aliases[]
  references[]
}
```

第一次确认的名字保存为 `name`，后续确认属于同一主体的其他名字、昵称和简称保存为 `aliases`。职业、社会身份、物种细分、隶属关系和主体之间的联系进入 fact 或 event。设备、地点和项目保留在普通记忆内容中。

`@user` 是当前 API 用户的稳定主体。模型看到可读名称和证据，程序负责持久 ID 映射。同一名称匹配多个 ID 且当前证据不能区分时，结果保持 pending。未确认身份不会改写原文中的人物归属。

## Memory 与图

Memory 是稳定的逻辑记录，Revision 是该记录在某一来源下形成的版本：

```text
Subject ──mentions──> Revision <──supported_by── Source
Memory  ──current──> Revision ──revises──> previous Revision
Revision ──related──> related Revision or Memory
```

四类 Memory 的职责：

| 类型 | 内容 |
|---|---|
| fact | 持续成立的事实、身份、关系或状态 |
| preference | 喜好、厌恶、选择倾向及其条件 |
| experience | 对经历的总结、认识和形成的经验 |
| event | 有发生、计划、取消、完成或变化时间的事情 |

同一 Memory 的补充、更正、改期、完成和取消形成新 Revision。原始 Source 一直表示“当时说过什么”；当前有效结论由版本链解析。历史补录按来源时间回放结构字段、参与主体、来源和边。事件的相对日期仅使用原始消息时间作锚点；锚点缺失时保留原始表达和未知精度。

## Search

```text
query
  → 基础词法／向量召回
  → LLM Planner 生成证据目标
  → 角色召回与充分度判断
  → 必要时同范围宽化
  → Source／Revision 命中聚合为逻辑 Memory
  → 按查询时间解析有效版本
  → 从高相关种子做有界图扩展
  → Memory 级 RRF
  → Jev 证据贡献排序
  → data[]
```

Planner 只判断查询模式和最多两个语义目标：

| 模式 | 主要证据目标 |
|---|---|
| direct | 直接答案和必要细节 |
| verify_premise | 核验问题前提及实际发生内容 |
| action | 影响行动可行性的约束与当前状态 |
| temporal_join | 先定位时间锚点，再找之前、之后或同日细节 |
| current_state | 当前状态及解释该状态的条件 |

程序补齐检索机械字段和必需证据角色。各通道独立召回，RRF 只融合逻辑 Memory 的名次。任一历史 Revision 或支持 Source 命中后，系统激活对应 Memory，并沿 `revises` 解析查询时间点有效版本。图扩展从各通道高相关 Memory 出发，使用有限深度和数量预算；canonical user 不参与全库桥接。

Jev 查看原问题、检索目标、候选当前视图和原文证据，按回答贡献排序。Jev 超时、部分返回或不可用时，已完成候选保留，缺失部分沿用 RRF 顺序。

## 故障与恢复

- Source 在模型调用前保存；请求检查点记录主体召回、主体输出、候选、目标选择和应用状态。
- 外部模型调用位于数据库事务外；最终主体、Memory、图、索引和应用检查点在一个提交中写入。
- 相同 request_id 和相同输入返回已有结果；内容冲突返回 409。
- 校验反馈只包含字段级错误和允许范围。一次修正仍失败时，原始输出、原因和检查点保留。
- 索引或提交前故障重试会复用已完成模型输出。
- 另一 pipeline 留下的未完成检查点通过原始 Source 重放进入当前 pipeline。

## 存储和接口

系统只使用六个既有 SQLite 表：`sources`、`memories`、`objects`、`edges`、`requests`、`search_documents`。主体、Memory、Revision 和检查点保存在这些表的现有记录结构中。本版本没有增加表。

公开接口为 `GET /health`、`GET /ready`、`POST /add`、`POST /search`。健康接口返回 `0.2.0` 和 `remi-0.2.0`。Search 的 `top_k` 范围是 1–100。

## 验证边界

当前结构回归为 450 passed。该结果验证协议、范围、版本、图、存储、恢复和接口执行合同。最新付费 Golden 仍是推广前的 V56 历史基线；`remi-0.2.0` 的新 Golden 需要单独执行和报告。历史架构与 Golden 文档保留其原版本号，现行运行规则以本文为准。
