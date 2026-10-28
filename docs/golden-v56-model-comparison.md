# V56 Evidence Slot Planner 双模型 Golden

日期：2026-10-28。写入 pipeline：`G11-append-first-memory-v53`。Search：V56 Evidence Slot Planner 与 V55 Objective-Aware Jev。运行时代码指纹：`e79ae38499c1aa60deaece4e7bf6b716dbdfd629379eda49a85c1d2ea2a38532`。两轮使用 V55 相同数据集与 TraceWeave 基线，分别从空的六逻辑集合 `SubjectMemoryStore` 开始。物理数据库和新增表均为 0。

V56 让 Planner 输出查询模式、证据角色和检索目标，程序补全检索字段。`verify_premise` 固定使用 `premise_check`，`action` 固定使用 `constraint` 与 `latest_state`，`temporal_join` 固定使用 `temporal_anchor` 与 `answer_detail`。协议错误携带逐字段信息修正一次，随后按查询模式生成有界计划。候选通过逻辑 Memory 级 RRF 合并，Jev 保持最终排序权。

## 总体结果

| 模型 | R@1 | R@3 | R@5 | R@10 | 全证据 | 任一证据 | MRR | Add 均时 | Search 均时 | 生命周期 Top1 | 墙钟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-4o-mini | **53.85%** | **66.24%** | **77.35%** | **82.05%** | **9/13** | **13/13** | **0.8487** | 50.26s | 18.62s | **3/3** | 43.25min |
| DeepSeek Flash | 44.87% | 56.41% | 60.26% | 79.91% | **9/13** | 12/13 | 0.7532 | **16.11s** | **15.42s** | 2/3 | **16.32min** |

两模型的幂等 Add、请求 ID 冲突和六逻辑集合检查全部通过。GPT 为 9 题完整、4 题部分、0 题全漏；DeepSeek 为 9 题完整、3 题部分、1 题全漏。

## 与 V55 对比

### GPT-4o-mini

| 指标 | V55 | V56 | 变化 |
| --- | ---: | ---: | ---: |
| R@1 | 56.41% | 53.85% | -2.56pp |
| R@3 | 67.95% | 66.24% | -1.71pp |
| R@5 | 71.79% | 77.35% | +5.56pp |
| R@10 | 75.64% | 82.05% | **+6.41pp** |
| 全证据 | 9/13 | 9/13 | 持平 |
| 任一证据 | 11/13 | 13/13 | **+2题** |
| MRR | 0.7846 | 0.8487 | **+0.0641** |
| Add 均时 | 94.92s | 50.26s | -44.66s |
| Search 均时 | 26.33s | 18.62s | -7.71s |

V56 恢复了两道 V55 全漏题：错误工作坊前提从 0/9 提升到 3/9，明早快走计划从 0/2 提升到 1/2。其余 11 道主查询的证据召回比例保持一致。本轮 Add 延迟改善主要来自供应商状态差异，不能单独归因于 Search 改造。

### DeepSeek Flash

| 指标 | V55 | V56 | 变化 |
| --- | ---: | ---: | ---: |
| R@1 | 46.15% | 44.87% | -1.28pp |
| R@3 | 50.00% | 56.41% | **+6.41pp** |
| R@5 | 57.69% | 60.26% | +2.57pp |
| R@10 | 65.38% | 79.91% | **+14.53pp** |
| 全证据 | 8/13 | 9/13 | **+1题** |
| 任一证据 | 9/13 | 12/13 | **+3题** |
| MRR | 0.6923 | 0.7532 | **+0.0609** |

V55 的 16 次 DeepSeek Search 决策全部返回空 `steps`。V56 使用明确 `searches` 协议后，前提核验、行动限制和时间连接都产生了真实角色查询。逐题改善为：错误工作坊 0/9→2/9、聚合题 1/2→2/2、居家设备 0/2→1/2、CrossFit 0/3→2/3。

## Planner 与程序补搜

两轮均执行 16 次 Search Planner，包括 13 道主查询和 3 道生命周期查询。

| 诊断 | GPT-4o-mini | DeepSeek Flash |
| --- | ---: | ---: |
| 一次获得有效计划 | 11 | 14 |
| 进入协议修正 | 4 | 2 |
| 修正后仍由程序兜底 | 4 | 0 |
| Planner 服务不可用 | 1 | 0 |
| 角色检索步骤 | 23 | 24 |
| 程序宽化补搜 | 0 | 0 |

GPT 的四次协议失败都来自 `time_expression` 缺少原问题支持；首次输出和修正输出均被程序拒绝，随后按已识别模式生成完整计划。DeepSeek 的两次同类错误在一次修正内恢复。GPT 的 CrossFit 查询遇到 Planner 服务不可用，程序走 direct 兜底。

所有角色初次召回的最高 RRF 分数都位于 0.0293–0.0328，高于默认阈值 0.02，因此宽化补搜没有触发。词元覆盖不足的角色也被最高分条件判定为充分。V56 本轮增益来自证据槽位与角色查询；默认阈值下的自动补搜效果尚未进入 Golden 实证。

## Jev 与候选规模

GPT 主查询平均候选数 65.08，最大 89；12/13 道题完成全部 Jev 评分。CrossFit 的 58 个候选均未取得 Jev 分数，最终沿 RRF 返回。诊断记录 8 个 rerank `ProviderUnavailable`、1 个 Search Planner 超时及 8 个 Add 文本调用超时；两个 Add 单元在外层重试后完成。

DeepSeek 主查询平均候选数 90.08，最大 148；13/13 道题完成全部 Jev 评分。更大的候选池提升了 R@10，同时增加排序难度，MRR 仍低于 GPT。DeepSeek 仅有 5 次 Add 提取格式错误，均进入既有修正流程；Search 和 Jev 没有供应商错误。

## 四类主查询缺口

### 错误工作坊前提

题目错误地声称 1 月 20 日在 Eastwood Middle School 举行公开预算工作坊。正确证据是当天真实发生的市场和家庭活动，共 9 条。

- GPT 使用 `Actual activities involving the user at Eastwood Middle School on January 20`，命中 3/9。
- DeepSeek 使用 `Actual date and nature of the user's budgeting workshop at Eastwood Middle School`，命中 2/9。
- 两轮遗漏的原文都已形成正式记忆；绝大多数没有进入候选池。
- 角色最高 RRF 分数超过 0.02，程序将初次召回判定为充分，宽化补搜没有执行。

前提核验已经从支持错误前提出发转向查找实际活动，但查询仍保留了错误地点或错误活动，覆盖同日真实生活轨迹的能力有限。

### 明早快走计划

目标证据是同日扭伤脚踝和在 8th and Maple 冰面滑倒。

- GPT 的 constraint 查询表达行动能力限制，扭伤证据 rank 1；滑倒证据进入候选池后被 Jev 排出 Top10，最终 1/2。
- DeepSeek 查询聚焦晨间习惯、精力和 practical limits；两条目标均进入候选池，Jev 都未排进 Top10，最终 0/2。

这一题的主要缺口位于角色查询具体性和 Jev 对安全限制的排序，而非写入遗漏。

### 居家设备

两模型都召回“在家完成工作的设备”证据，遗漏“生日聚会期间 iPhone 持续故障”的时间锚点，最终均为 1/2。

- 两条目标原文都已形成正式记忆。
- iPhone 锚点没有进入最终候选池。
- GPT 的两次 Planner 输出都使用了问题未支持的时间表达，程序转为 temporal_join 通用计划；DeepSeek 一次生成有效 temporal_join。
- answer_detail 因 anchor 时间不唯一执行既有有界回退，仍只恢复答案细节。

当前缺口位于时间锚点召回与跨事件连接。

### CrossFit 行动建议

目标证据是 6 月背痛、7 月医生要求卧床、10 月拄手杖慢走。

- GPT 遇到 Planner 超时和 Jev 全部不可用，direct/RRF 只保留背痛，最终 1/3。
- DeepSeek 生成医疗限制与最新状态查询，卧床 rank 1、背痛 rank 7，拄手杖记录没有进入候选池，最终 2/3。

DeepSeek 的角色查询已经覆盖安全限制，剩余缺口是最新康复状态的候选覆盖。GPT 本题结果主要受服务降级影响。

## 生命周期

GPT 三题 Top1 全部通过。DeepSeek 的“从北京搬到上海最终何时完成”将 9 月 21 日的改期记录排在 Top1，9 月 24 日完成记录位于 rank 2／3，因此生命周期为 2/3。其余指定日期状态和当前状态均通过。该回归属于 temporal_join 在计划日期与完成日期之间的排序选择。

## 结论与边界

V56 达到两项核心目标：DeepSeek 可以稳定执行结构化角色查询；两模型的 R@10、任一证据覆盖和 MRR 均高于 V55。GPT 保持 9/13 全证据并消除全漏，DeepSeek 将全证据提高到 9/13。

当前边界集中在三处：默认 0.02 阈值使程序宽化补搜未进入实际链路；时间锚点召回仍会遗漏关联事件；大候选池下的安全限制排序仍有不稳定。DeepSeek 生命周期的完成日期 Top1 回归也属于同一时间连接边界。完整通过标准继续使用：13 道题保持当前总体指标，同时让弱角色召回真实触发有界宽化，恢复设备锚点和 CrossFit 最新状态，并保持生命周期 3/3。

## 运行完整性与材料

- 数据集 SHA-256：`af172edb259b55490dea78c7b01735ec85c9b895e04b050c9dcf24e30e9fe82e`
- 代码指纹：`e79ae38499c1aa60deaece4e7bf6b716dbdfd629379eda49a85c1d2ea2a38532`
- 离线验证：605 passed、15 skipped；针对性复测 48 passed、1 skipped。
- GPT：`.local-data/golden-v56-evidence-slot-20261028-v1/golden-gpt-4o-mini/`
- DeepSeek：`.local-data/golden-v56-evidence-slot-20261028-v1/golden-deepseek-flash/`
- 两个目录均包含 `result.json`、`records.json`、`run.json` 和 `diagnostics.jsonl`。

本轮代码和输入在运行期间保持冻结。临时 DeepSeek 凭据文件已删除。`scripts.analyze_golden` 仍读取旧字段 `top1_expected_evidence`，与当前结果结构不兼容；Golden 评分、结果和 trace 均完整，报告直接从 `result.json`、`records.json` 和 diagnostics 汇总。
