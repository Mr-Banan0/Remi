# V55 Objective-Aware Jev Search 双模型 Golden

日期：2026-10-27。写入 pipeline：`G11-append-first-memory-v53`。Search：V55 objective-aware Jev ranking。运行时代码指纹：`951e0fdbd59bed43c55ef2f4fc14d6d67531c205380190aff90be9b29b17a385`。数据集与 V54 相同，两轮分别从空的六逻辑集合 `SubjectMemoryStore` 开始，物理数据库和新增表均为 0。

V55 保留 direct、premise_check、constraint、temporal_anchor、answer_detail、latest_state 多角色召回与逻辑 Memory 级 RRF。每条候选向 Jev 提供命中的角色及该分支实际查询；普通角色不再获得强制置顶。`first`／`latest` 时间运算继续在 Jev 验证活动后由程序执行。Jev 得分不完整时整题保持 RRF 顺序。

## 总体结果

| 模型 | R@1 | R@3 | R@5 | R@10 | 全证据 | 任一证据 | MRR | Add 均时 | Search 均时 | 生命周期 Top1 | 墙钟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-4o-mini | **56.41%** | **67.95%** | **71.79%** | **75.64%** | **9/13** | **11/13** | **0.7846** | 94.92s | 26.33s | 3/3 | 79.59min |
| DeepSeek Flash | 46.15% | 50.00% | 57.69% | 65.38% | 8/13 | 9/13 | 0.6923 | **18.03s** | **16.83s** | 3/3 | **18.14min** |

两模型的幂等 Add、请求 ID 冲突和六逻辑集合检查均通过。GPT 主集只有错误工作坊前提和明早快走计划两题全漏；设备题命中 1/2，CrossFit 命中 1/3，其余 9 题全部证据命中。DeepSeek 的 16 次 Search 决策继续全部返回空 `steps`，因此其结果仍然代表直接召回、基础图路径和 Jev，而没有执行角色补搜。

## 与 V54 GPT 对比

| 指标 | V54 | V55 | 变化 |
| --- | ---: | ---: | ---: |
| R@1 | 23.93% | 56.41% | **+32.48pp** |
| R@3 | 45.73% | 67.95% | **+22.22pp** |
| R@5 | 57.26% | 71.79% | **+14.53pp** |
| R@10 | 76.07% | 75.64% | -0.43pp |
| 全证据 | 8/13 | 9/13 | +1题 |
| 任一证据 | 12/13 | 11/13 | -1题 |
| MRR | 0.6218 | 0.7846 | **+0.1628** |
| Search 均时 | 24.31s | 26.33s | +2.02s |

MRR 改善与本次设计直接一致：Principal Carter、Alicia、图书、Jasper 训练缺席等正确证据从 V54 的 rank 2／6 恢复到 rank 1。Jasper 康复锚点与 Mrs. Patel 活动从 1/2 提升到 2/2，分别位于 rank 3 和 rank 1。角色召回仍负责把证据送入候选池，普通角色不再覆盖 Jev 的排序。

R@10 基本持平，因为两项变化互相抵消：Jasper 康复题增加一条证据；错误工作坊题失去 V54 强制置顶带来的 2/9 偶然命中，CrossFit 失去一条旧版角色置顶命中。V54 与 V55 分别重新执行完整 Add，GPT Memory 数为 256→265、revision 为 310→317，因此完整 Golden 同时包含文本模型生成波动；具体归因以候选和 Jev trace 为准。

## Jev 表现

GPT 的 13 道主查询中有 11 道取得完整 Jev 分数。完整评分题中，目标候选在 8 道题位于 Jev rank 1；跨全部有分数的目标／非目标候选，成对区分 AUC 为 0.8355。该 AUC 受两道明确失败题显著拖累，且 V54／V55 的写入结果和候选集合不同，不用于单独归因 Prompt。

改善样本：

- Principal Carter 证据 Jev=0.94，rank 1；V54 曾被 0.10 的强制角色项压到 rank 2。
- Alicia 两次 workshop input 被同一逻辑 Memory 聚合，Jev=0.83，rank 1。
- 图书证据 Jev=0.33，仍明显高于其余候选，V54 的服务失败曾使它落到 rank 6。
- Jasper 两次因照顾而放弃训练的证据 Jev=0.85／0.69，位于 rank 1／2；V54 部分评分回退时首条目标在 rank 6。
- Jasper 康复锚点 Jev=0.61、Mrs. Patel 活动 Jev=0.69，分别位于 rank 3 和 rank 1，完成多跳证据覆盖。

仍然失败的语义与召回边界：

1. **错误工作坊前提：0/9。** 九条同日真实活动中六条进入候选，但 Jev 只有 0.02–0.05，首个目标约为 Jev rank 29。Planner 的两个查询仍然要求检索“Eastwood workshop 学生反馈”，继续沿错误前提找支持证据。Objective-aware Prompt 可以判断候选贡献，无法替 Planner 把查询改写成“1 月 20 日实际发生了什么”。
2. **明早快走计划：0/2。** 一条伤病证据进入 direct、constraint、answer_detail 三类候选，Jev=0.22，约为 rank 32；另一条没有形成目标候选。Planner 的 constraint 查询是“过去的晨间习惯或步行习惯”，没有表达伤病、行动限制或医疗风险。该步骤还错误使用 `select=latest`，使程序时间运算优先了一条非目标记录。主要缺口仍在角色内查询和时间操作选择。
3. **居家设备：1/2。** 工作设备证据 Jev=0.78、最终 rank 5；iPhone 故障锚点虽然进入 temporal_anchor 候选，Jev 只有 0.12–0.16。Planner 使用了错误的 4 月 14 日锚点，真实承接关系没有建立。
4. **CrossFit：1/3。** 6 月背痛证据进入候选并以 Jev=0.73 排第 1；7 月卧床和 10 月手杖均未进入候选。Planner 查询仍聚焦“过往力量训练和备赛经历”，没有覆盖当前身体限制。该题属于角色查询生成和召回缺失，Jev 无法恢复未出现的证据。

## 服务稳定性与延迟

GPT 主查询平均候选数为 71.46，V54 为 69.62。13 道题中两道 Jev 完全不可用：第一题 0/64、第十二题 0/71；两题依靠 RRF 仍完整命中。GPT 全程记录 131 个 rerank 批次，其中 24 个出现 ReadTimeout 或 ProviderUnavailable；DeepSeek 记录 121 个批次，其中 20 个出现同类错误，主集有两道部分评分后回退 RRF。

GPT Add 均时上升到 94.92 秒，主要由文本端点的 33 次 APITimeoutError 和外层 10 次可恢复 Add 重试造成；最长批次超过 300 秒。该延迟属于本轮供应商观测，不能归因于 Search Prompt。Search 均时增加 2.02 秒，候选规模与 reranker 错误继续是稳定性瓶颈。

## DeepSeek 边界

DeepSeek 的 R@10、全证据、任一证据和 MRR 与 V54 完全相同；R@3 和 R@5 分别下降 7.69pp、3.85pp。16 次 Search 决策仍然全部为空步骤，新角色查询和 objective metadata 没有被实际使用。主集第六题为 8/44 部分评分、第十三题为 32/90 部分评分，均回退 RRF。DeepSeek Memory 数为 464、revision 为 495，仍显著多于 GPT 的 265／317。

## 判定

删除普通角色强制置顶应当保留。V55 将 MRR 提升到 0.7846，并使 9/13 题完整命中；这说明角色负责召回、Jev 负责普通语义排序的职责划分更稳定。Objective-aware Prompt 改善了多跳锚点与多个旧 rank 2／6 样本，但不能修复错误的角色内查询、缺失候选或服务超时。后续 Search 优化应集中在三项完整范围：角色查询表达约束与反证目标、Planner 对 `first/latest` 的合法使用、rerank 候选规模与完整评分率。完成标准为保留 V55 的 MRR 和 9/13 全证据，同时恢复错误前提与 CrossFit 的候选覆盖，并降低 Jev 降级率。

## 材料

- GPT：`.local-data/golden-v55-objective-jev-20261027-v1/golden-gpt-4o-mini/`
- DeepSeek：`.local-data/golden-v55-objective-jev-20261027-v1/golden-deepseek-flash/`
- 两个目录均包含 `result.json`、`records.json`、`run.json` 和 `diagnostics.jsonl`。

本轮代码和输入在运行期间保持冻结。Golden 完成后只更新报告与当前架构说明，工作区保持未提交。
