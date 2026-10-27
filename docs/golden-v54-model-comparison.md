# V54 Role Search 双模型 Golden

日期：2026-10-27。写入 pipeline：`G11-append-first-memory-v53`。Search：V54 evidence-role retrieval。运行时代码指纹：`5bb26d35df226886a929e756ab401e791817c6b07873f32be0e6f75e36e895d9`。数据集 SHA-256：`af172edb259b55490dea78c7b01735ec85c9b895e04b050c9dcf24e30e9fe82e`。

两轮使用相同 dataset-v1、TraceWeave 基线、Embedding、Jev reranker、候选预算和 13＋3 道查询。GPT 使用现有中转站的 `gpt-4o-mini` 与 JSON Schema；DeepSeek 使用官方 `deepseek-flash`、关闭 thinking 并使用 JSON object。两轮各自从空的六逻辑集合 MemoryStore 开始，物理数据库和新增表均为 0。

## 总体结果

| 模型 | R@1 | R@3 | R@5 | R@10 | 全证据 | 任一证据 | MRR | Add 均时 | Search 均时 | 生命周期 Top1 | 墙钟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-4o-mini | 23.93% | 45.73% | 57.26% | **76.07%** | 8/13 | **12/13** | 0.6218 | 58.32s | 24.31s | 3/3 | 60.25min |
| DeepSeek Flash | **46.15%** | **57.69%** | **61.54%** | 65.38% | 8/13 | 9/13 | **0.6923** | **19.73s** | **13.43s** | 3/3 | **19.97min** |

GPT 覆盖更多证据，但证据排名更分散；DeepSeek 命中的证据更靠前，同时有 4 道题全漏。两模型的幂等 Add、请求 ID 冲突和六逻辑集合检查均通过。

## 与 V53 GPT Golden 对比

| 指标 | V53 | V54 GPT | 变化 |
| --- | ---: | ---: | ---: |
| R@10 | 70.51% | 76.07% | +5.56pp |
| 全证据 | 8/13 | 8/13 | 0 |
| 任一证据 | 10/13 | 12/13 | +2题 |
| MRR | 0.7115 | 0.6218 | -0.0897 |
| Search 均时 | 20.48s | 24.31s | +3.83s |

同题变化集中在两处：1 月 20 日错误工作坊前提从 0 提升到 2/9；Jasper 康复散步题从 0 提升到 1/2。其他 11 题的证据召回比例保持。V53 与 V54 分别重新运行完整 Add，GPT 本轮形成 256 条 Memory／310 个 revision，V53 为 241／302；因此完整 Golden 差异同时包含生成波动和 Search 改动，逐题 trace 用于判断具体环节。

## Search 角色执行

GPT 的 16 次查询决策均形成角色步骤：12 次返回 2 步，4 次返回 1 步，共包含 answer_detail 10、temporal_anchor 6、premise_check 5、constraint 3、direct 2、latest_state 2。V54 的角色召回、逻辑 Memory RRF 和角色候选保留在真实链路中实际执行。

DeepSeek 的 16 次查询决策均成功返回 JSON 和 query_mode，但 `steps` 全为空。其成绩来自直接召回和基础图扩展，没有执行 premise_check、constraint、temporal_anchor、answer_detail 或 latest_state 补搜。该现象属于 Search 计划遵循缺口；当前程序把语法合法的空步骤视为有效计划，所以没有触发 planner unavailable 兜底。

## GPT 未全命中题目

### 错误工作坊前提：2/9

模型正确选择 `verify_premise`／`premise_check`，实际查询仍沿用了“Eastwood Middle School 工作坊及学生反馈”这一错误前提。1 月 20 日真实内容是家庭市场、Mrs. Patel 和共同做饭。角色分类生效，查询构造继续寻找支持证据，只偶然返回两条同日内容。

### 明早快走计划：0/2

constraint 查询为“过去的晨间习惯或步行习惯”，没有表达脚踝受伤、行动限制或健康风险。直接覆盖达到 0.75，角色通道依然执行，但错误的约束查询没有召回两条伤病证据。根因位于角色内查询生成。

### iPhone 故障后的居家设备：1/2

answer_detail 找到居家工作设备证据并排第 4；temporal_anchor 使用了错误／无法确认的事件时间，随后执行无时间约束回退，生日聚会中手机继续故障的锚点没有进入最终候选。设备答案已找到，事件承接证据缺失。

### Jasper 康复散步后的 Mrs. Patel 活动：1/2

Mrs. Patel 活动排第 1。Jasper 康复散步证据已经进入 base 和 temporal_anchor 候选，reranker 分数仅为 0.30／0.33，最终未选入 Top10。该题的主要瓶颈从 V53 的“未形成有效补搜”移动到最终筛选。

### CrossFit 与恢复限制：2/3

constraint 找到背痛和卧床限制，分别排第 7 和第 2。latest_state 查询描述“最新健身水平和训练活动”，没有覆盖 10 月仍需手杖的恢复状态，因此第三条证据没有进入任何召回通道。角色存在，查询表达范围仍偏窄。

## DeepSeek 结果边界

DeepSeek 主集形成 455 条 Memory、486 个 revision、714 个 search document；GPT 为 256、310、538。DeepSeek 的类型分布为 event 245、fact 97、preference 57、experience 56，显示同一输入被拆成更多独立记忆。GPT 分布为 experience 93、event 80、preference 51、fact 32。

DeepSeek 在 8 道题全证据命中，另 1 道部分命中；两个 misleading、一个 hallucination 和一个 fact 全漏。由于所有查询计划步骤为空，这轮结果适合衡量 DeepSeek 写入加直接检索的表现，不能证明 DeepSeek 已执行 V54 角色 Search。

## 稳定性与调用

| 项目 | GPT-4o-mini | DeepSeek Flash |
| --- | ---: | ---: |
| 总输入 token | 2,226,850 | 2,073,878 |
| 总输出 token | 61,818 | 94,114 |
| Search 决策均时 | 8.47s | 1.49s |
| Memory | 256 | 455 |
| Revision | 310 | 486 |
| Edge | 890 | 1,219 |

GPT 运行期间三次从快照恢复，主要原因是 Embedding／文本端点 TLS 和超时；DeepSeek 首批主体调用中断后恢复一次。所有 163 个单元、13 道主集查询和 3 道生命周期查询最终完成。供应商错误和恢复尝试保留在各自 `diagnostics.jsonl` 与 `result.json` 中。

## 材料

- GPT：`.local-data/golden-v54-role-search-20261027-v1/golden-gpt-4o-mini/`
- DeepSeek：`.local-data/golden-v54-role-search-20261027-v1/golden-deepseek-flash/`
- 两个目录均包含 `result.json`、`records.json`、`run.json` 和 `diagnostics.jsonl`。

本轮代码和输入在运行期间保持冻结，Golden 结束后未修改实现。工作区保持未提交。
