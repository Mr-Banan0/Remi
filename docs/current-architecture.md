# 当前实验架构：V45 无类型主体连线与关系记忆

实验入口为 `experiments.subject_engine.SubjectGraphEngine`，pipeline 为 `G11-untyped-subject-links-v45`。

原文 → 主体候选 → 主体身份与无类型连通边维护 → 固定身份 → 主体范围内召回 → 置信度不足时补入无主体记忆并统一排序 → 四类记忆维护 → 校验、修订回放与原子提交。持续关系作为 Fact 维护，关系的开始、变化或结束作为 Event 维护。只绑定其他主体的记忆保持在本轮 Add scope 外。

事实、偏好、经验和事件支持创建、更新、保持、待定。同一记录更新保留 ID 和来源修订；事件支持计划／实际两项时间、参与者增减和关联撤销。程序在记忆模型调用前完成召回与补搜，正常保持两个模型调用，共享一次协议修正。每次 Add 最多重审两条待定，共用请求截止时间。

历史补录重建结构字段与图投影。原始正文保存在变更记录；发生历史分支时，用带来源日期的叙述保存完整文本依据。Search 沿有效事件边扩展最多两跳、24 个事件节点。主体导航、全局混合检索和重排继续保留。

V45 的职责边界见 [无类型主体连线与关系记忆](architecture-changes/g11-untyped-subject-links-v45.md)，召回边界见 [V44 程序化无主体补搜](architecture-changes/g11-programmatic-unbound-recall-v44.md)。V43 的闭环设计与离线结果继续保留为历史基线。生产 API 仍使用下方 V32，实验推广单独验收。

---

# 生产 V32 架构记录：维护主体与关系，提取四类记忆后追加保存

当前 pipeline 为 `G11-readable-subjects-v32`。每个用户文本单元共用召回一次，依次执行主体与关系维护、四类记忆提取，最后由程序校验并保存。正常模型调用为两次。

## 类型与边界

| 内容 | 类型 | 职责 |
| --- | --- | --- |
| 主体 | person／animal／organization | 具体人、动物、组织／团队的稳定身份及有来源的别名 |
| 关系 | relation | 两个主体之间有原文依据的联系，保存端点、角色方向与来源 |
| 事实 | fact | 原文表达的信息、属性与状态，保留条件和时间范围 |
| 偏好 | preference | 原文表达的选择倾向、喜好或优先级，保留适用条件 |
| 经验 | experience | 原文根据经历明确总结的教训、方法或结论，保留依据与适用条件 |
| 事件 | event | 一次发生、计划或变化陈述，保留参与者、状态和时间 |

具体经历属于事件；从经历中总结的认识属于经验。事实、偏好和经验可以独立保存。设备、地点、项目等名称保留在记忆内容。原文中的意愿、假设、请求与引用归属通过 mode、context 和证据继续保留。

## 写入步骤

| 步骤 | 输入 | 输出与处理 |
| --- | --- | --- |
| 保存来源 | API 用户、会话、消息与时间 | 不可变 Source 和请求检查点 |
| 共用召回 | 原文、同会话前文、用户与知识时间 | 同一份主体、关系、四类记忆和来源快照 |
| 主体与关系模型 | 原文、共用候选及来源 | 本轮身份 ref，主体 create／reuse／pending 及别名提议，关系新增／更新／保持／待定 |
| 固定身份 | 主体与关系输出 | 程序验证身份与别名各自的来源、归属、引用、端点与方向；生成稳定主体 ID，追加有效别名 |
| 四类记忆提取模型 | 原文、固定主体视图、同一批历史 | 每项直接输出类型、内容、主体 ref、表达模式、条件、事件状态、日期表达及来源 |
| 编译写入计划 | 提取结果及身份校验结果 | 来源、类型、主体引用和日期规则通过的项目生成新增操作；未确认主体及无依据日期进入待定 |
| 原子提交 | 主体、关系操作、四类记忆与检索文档 | 记录、图边、索引与应用检查点一起提交 |

主体及关系正常一次调用，四类记忆提取正常一次调用，合计两次。两个阶段共享最多一次协议修正；供应商网络重试与并发冲突重算单独统计。纯主体／关系输入的提取结果可以为空数组，调用预算仍为两次。

主体输出为统一 subjects 和 connections。subjects 操作为 create／reuse／pending；reuse 同时承载可选新别名。程序分别验证身份与别名，无效别名写入 subject_alias_issues，已确认身份继续供记忆和关系使用。主体与别名证据只输出 source_id，由程序复制原文；reuse 按所选身份的已知名字集合校验。输入采用固定任务提示词与分区文本：当前原文、必要上下文、主体候选及身份依据、已有关系、召回记忆和来源。主体与关系 few-shot 使用同一文本渲染器，返回继续采用 JSON Schema。机器时间戳及名称命中元数据保留在程序状态。完整变更见 [V32](architecture-changes/g11-readable-subjects-v32.md)。

## 四类记忆的保存规则

提取输出字段为 record_type、content、subject_refs、mode、context、status、time_expression、evidence。类型限定为 fact、preference、experience、event。程序从已提供的原文复制证据，分配记录与修订 ID，生成 mentions 和 supported_by 图边。

事实、偏好、经验的 status、time_expression 为 null，时间条件保留在内容中。事件提供状态与原文支持的日期表达；时间未知时使用 null。主体引用来自当前固定视图；依赖待定主体的项目保留在待定检索索引。

每项有效提取结果追加一条独立记录。相同请求重试保持幂等；不同来源重复表达可以生成独立记录。改期、完成、取消作为新的事件陈述保存，原有事件与偏好等记录维持原样。跨来源去重、归并和普通事件生命周期修订属于后续明确设计范围。

主体关系继续由第一阶段维护，稳定关系 ID 可以追加 update 修订。四类记忆提取合同只包含内容描述与来源。旧的最后一次记忆维护模型、其提示／示例／操作 DTO，以及普通事件 patch 写入路径已经退出，原因是当前用户选择先建立直接保存闭环。

## 召回、证据与存储

共用召回沿用原文混合检索、名称／别名线索、旧卡参与者和一层联系，最多八张历史卡、24 个主体，候选包最多 24,000 字节。保留卡维持完整参与者与来源；relation_catalog 是同批 history 中主体关系的视图。提取阶段使用历史解释当前原文中的回指。

context_sources 来自同用户、同会话已有的最近两条原始 user／assistant 消息，以及当前单元的 assistant 回复，合并后最多三条。同会话没有前文且本次只有用户输入时为空。

关系投影为 related，四类记忆通过 mentions 连接主体，supported_by 保留来源，关系修订通过 revises 连接。Search、冷热访问、Jev 重排与时间计算保持当前流程。

沿用 sources、memories、objects、edges、requests、search_documents 六表与 JSON schema_version=13。检查点改为 `G11-readable-subjects-v32`，旧 pipeline 的请求通过明确原文重放进入当前链路。已有实验及冻结结果保持原状。

程序验证覆盖来源、引用、归属、类型、时间支持和图一致性。主体语义归类、同名／别名判断与提取内容准确性继续作为真实模型验收项目。本轮离线结果见 [验证记录](verification.md)，变更边界见 [V32](architecture-changes/g11-readable-subjects-v32.md)。


最近一次完整Golden：冻结V29已完成，原文证据R@10为69.23%，主集保存69条四类记忆；主体语义与后续事件写入仍未闭环。完整结果见 [V29 Golden](golden-g11-v29-results.md)。

当前主链路为 V32，专项结果见 [V32 报告](subject-v32-results.md)。V33 主体身份与无向连接为独立实验，主 src 保持 V32；实验结果与边界见 [V33 报告](subject-v33-results.md)。

V34 主体实验统一为 individual／organization，并分别保存别名与有来源的描述。实验验证描述召回和跨轮身份；生产主链路维持 V32。设计边界见 [V34](architecture-changes/g11-individual-descriptions-v34.md)。

V34 最终主体验证：离线363 passed，真实28组18/28、扩展10组6/10、原跨轮3/4、描述跨轮1/4。结果与具体失败见 [V34报告](subject-v34-results.md)。

## V35 完整实验与生产入口

生产 `src` 服务入口继续为 V32；当前完整实验 `G11-reference-evidence-v35` 已贯通 individual／organization、来源描述、当前回指／历史身份依据、无向主体连接及四类记忆 Add／Search。新增 reference 校验替换当前引文完整包含旧名／描述的门槛；实验使用 MemoryStore，实际六集合快照可审计。两组 Golden 已完整结束，结果见 [V35 对照](golden-v35-model-comparison.md)，设计与边界见 [V35 架构](architecture-changes/g11-reference-evidence-v35.md)。本轮保持未提交。

## V36 主体校验与修正闭环

当前完整实验为 `G11-structured-repair-v36`，增加定位到字段的校验、上一轮输出、已通过部分保护和未确认提议撤回；正常两次调用及共享一次修正预算保持。生产入口维持V32。最近完整Golden仍为V35，本轮仅验证主体。设计见 [V36架构](architecture-changes/g11-structured-repair-v36.md)，离线407项及真实模型结果见 [V36报告](subject-v36-repair-results.md)。


## V37：关键词主体召回与两条上下文

当前完整实验为 `G11-keyword-subject-recall-v37`：已有 user 加最多五个关键词候选，名称／别名优先于描述；最近两条先前消息处理指代。主体模型使用精简身份卡与少量原文依据；历史记忆供后续四类记忆模型读取，同一快照与正常两次调用保持。历史参与主体优先并从尾部裁主体的逻辑由先固定关键词主体、再装入记忆的流程替代。生产入口继续 V32；完整离线427项通过，318条旧原文候选重放完成，真实模型及Golden本轮未重跑。详见 [V37架构](architecture-changes/g11-keyword-subject-recall-v37.md) 和 [验证报告](subject-v37-keyword-results.md)。

## V37 双模型完整 Golden（2026-10-23）

同一冻结V37已完成双模型Golden。DeepSeek原文证据R@10 69.23%、9/13全证据命中、生命周期3/3、13.32分钟；GPT为73.08%、9/13、3/3、39.60分钟。主体同标签重复新增ID诊断14→0、3→0，但DeepSeek有机构合并和描述归属问题，GPT主集仅user／Monica／Jasper三个主体，明确新参与者漏建。摘要可见与原文回传分别审阅，人工语义问题与结构审计分别报告。代码和prompt全程冻结，生产入口V32、历史结果保留，Git未提交。详见 [完整对照与bad case](golden-v37-model-comparison.md)。


## V38 主体提示词实验

当前实验pipeline为`G11-subject-prompt-v38-r4`。共用召回→主体维护→程序固定身份→四类记忆提取的链路与正常两次调用预算保持。主体输入按当前原文、两条上下文和附B依据的身份卡直接渲染，9组独立示例与实时输入分别标记，修正规则按需提供。433项离线检查通过；真实双模型结果和各轮回退记录见[验证报告](subject-v38-prompt-results.md)。生产入口保持V32。


## V39 GPT 精简协议对照与回退（2026-10-23）

V39-R1／R2各完成三轮GPT对照，旧回归均值29／46、32／46，低于本次V38三轮均值34.33／46。R2的操作分支Schema消除了观测到的字段组合违约，修正次数19→11；基本用户身份回退、当前／历史混淆与摘录误拒仍在，质量门槛未通过。活跃实验已恢复`G11-subject-prompt-v38-r4`，生产V32保持；完整原型及九轮trace存于`.local-data/subjects-v39-gpt-20261023-v1/`。保留原总分和独立身份／描述／联系审计，当前未重跑Golden、未commit。详见[V39 GPT报告](subject-v39-gpt-results.md)。


## V40：按名称ID数量切换维护顺序（2026-10-23）

当前完整实验为`G11-name-collision-routing-v40`。当前原文同一名字／别名命中至少两个不同ID时先记忆后主体，其余先主体后记忆。程序保全同名候选，复用共享召回；记忆优先阶段提供参与者ref、历史定位与当前四类内容，主体阶段保留ref后维护身份、别名、描述与连接。正常两次调用，共享一次修正，保持检查点与原子保存。生产入口保持V32；本轮真实主体质量尚未通过稳定性验收。

离线503项通过。GPT完整Golden来源R@10为69.23%、全证据8/13、生命周期3/3；主集152次主体优先、7次记忆优先。实际Alicia别名漏存造成重复ID，记忆优先3次计划清空，主体仍不稳定。通用场景三轮最终主体验收18/24、全部维度16/24。完整设计见[V40边界](architecture-changes/g11-name-collision-routing-v40.md)，结果与原文trace见[V40报告](subject-v40-gpt-results.md)。本轮未commit，存储继续使用原有六集合。

## V41：主体证据与参与者交接（2026-10-23）

当前实验为 `G11-subject-closed-loop-v41`，生产入口维持V32。主体阶段选择程序编号的C原文片段及属于候选ID的B依据，程序复制引用并独立保存references；模型显式归属当前名字，程序校验追加aliases。主体优先记忆输入使用稳定P{ref}，历史人物仅作证据标签；来源／handle错误保留原答与具体字段反馈进入共享修正。当前原文有效引用与实际人物归属分别评价，同名路由及两次正常调用保持。设计边界见[V41](architecture-changes/g11-subject-closed-loop-v41.md)。

## V41真实验证结果（2026-10-23）

最终590 passed、0 skipped，16.32秒。固定runtime-r2的GPT三轮完成24组72步，150份JSON回复；身份整组12/24、全维度7/24、最终协议72/72，正常两次调用与共享修正预算全过。中文短名漏存、明确同名新人判断、当前user漏提及历史人物误带仍存在。报告者entity_ids的评分边界与引用归属问题单列。预先语义门槛未通过，Golden本轮未启动。生产src保持V32、物理新表0、工作区未commit。详见[V41结果](subject-v41-gpt-results.md)。

## V41-context1：最近一条前文（2026-10-23）

当前实验pipeline为 `G11-subject-closed-loop-v41-context1`，共用上下文取同会话当前原文之前最近1条user／assistant原始消息。主体候选召回、主体渲染、两种记忆入口及其修正调用共用CONTEXT_LIMIT；B身份依据与历史记忆继续按各自证据范围提供。检查点使用独立pipeline，旧实验快照保持原状。离线592 passed、0 skipped，13.18秒；本次真实模型与Golden调用0，Git未提交。验证记录位于`.local-data/subject-context1-v41-20261023-v1/verification.json`。
