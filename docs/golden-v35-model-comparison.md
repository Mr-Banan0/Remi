# V35：主体回指修复后的两模型 Golden 对照

日期：2026-10-22。两组使用同一冻结实现 `G11-reference-evidence-v35`；生产服务入口维持 V32。本轮工作区保持未提交。真实模型实验创建物理数据库与表均为 0；离线 SQLite 回归沿用已授权六表，仅在测试临时目录运行。

## 完整 Golden 结果

| 模型 | 证据 R@1 | 证据 R@5 | 证据 R@10 | 全证据命中题 | 生命周期原文 Top1 | 总墙钟耗时 | 主集平均每条写入 | Add 文本请求（基础＋修正） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| deepseek-flash | 50.00% | 69.23% | 69.23% | 9/13 | 3/3 | 14.13 分 | 4.60 秒 | 326+15 |
| gpt-4o-mini | 42.31% | 67.95% | 67.95% | 8/13 | 2/3 | 48.94 分 | 16.95 秒 | 326+115 |

R@K 对每题应召回的**原始来源证据**计分，再对 13 题取平均。全证据命中表示该题全部指定原文进入前 10。该实验执行记忆 Add／Search，最终自然语言回答准确率未计分。单条平均写入用主集所有 Add 批次耗时之和除以 159；原始报告另存批次延迟分布。文本请求数统计实际物理尝试，包含失败尝试；正常逻辑 Add 调用为 163×2。生命周期的 Top1 核对返回的实际原文，四步内容仍按来源追加保存。

历史 V29 GPT Golden 的 R@10 为 69.23%（9/13）；与本次之间包含主体结构、完整实验入口等多项改动，历史分数作为整体参照。两模型在本版同时承担主体维护、四类记忆提取和 Search 决策，结果反映各自完整链路。

## 修复与离线验收

复用身份输出 `reference={source_id,mention,basis_id}`：当前 source_id 与短 mention 指向本轮原文，B 编号指向所选旧 ID 的可见名字或描述及原始来源。程序核对来源、文字、归属与可见性，模型结合上下文判断身份。创建和待定使用 null。旧的“当前引文必须完整包含旧名或旧描述”门槛已经退出。

**完整离线 378 passed，0 skipped，12.83 秒**。覆盖护士回指、错误依据归属、伪造提及、历史／未来／跨用户证据、歧义待定、完整 Add→四类记忆→Search、提交原子性、失败续跑、幂等、快照、共享一次修正与既有 HTTP 合同。真实 DeepSeek 描述跨轮中，`The nurse` 选中正确历史 ID 后已通过校验；该组仍因前一轮 `colleague`／`colleagues` 描述的字面校验失分。

完整实现与保留／退出边界见 [V35 架构记录](architecture-changes/g11-reference-evidence-v35.md)。程序验证结构与证据一致性，身份语义准确性继续由真实模型用例检查。

## 主体真实预检

| 组别 | DeepSeek | GPT‑4o‑mini |
| --- | --- | --- |
| 原 28 组单轮 | 25/28 | 25/28 |
| 独立扩展 10 组 | 9/10 | 9/10 |
| 原 4 组跨轮 | 4/4 | 1/4 |
| 描述相关 4 组跨轮 | 3/4 | 0/4 |

单轮结果存于 `golden-v35-reference-20261022-v1/`，跨轮与最终 Golden 存于 v2。两次冻结之间修改了 CLI 参数转发和完整 Search 的图种子测试；身份合同、提示、few-shot 与引用校验的四个核心文件逐一核对一致。部分验证脚本 summary 沿用 V34 实验名称，实际运行代码、输入输出合同与 manifest 对应 V35。单轮全过率与跨轮闭环分开报告；GPT 在跨轮中仍会把历史来源称呼填入当前 reference，或遗漏描述与联系。

## 最终写入与实际图

以下图规模仅统计 159 条主集；待定与修正统计包含四条生命周期输入，共 163 个单元。

| 指标 | DeepSeek | GPT‑4o‑mini |
| --- | --- | --- |
| 主体数 | 31 | 10 |
| 主体类型 | 个体 26；组织 5 | 个体 10 |
| 四类记忆数 | 402 | 181 |
| 记忆分类 | 事实 82；事件 130；偏好 74；经验 116 | 经验 70；事件 68；偏好 20；事实 23 |
| 无向主体联系 | 49 | 6 |
| 记忆／来源投影边 | 904 | 385 |
| 提取候选 | 495 | 281 |
| 待定候选 | 86 | 94 |
| 待定原因 | 日期依据未通过 75；主体未确认 11 | 非事件的时间字段未归入内容 14；日期依据未通过 21；主体未确认 59 |
| 最终身份校验拒绝 | 新主体名字缺少来源锚点 2；本轮提及与原文文字不符 10 | 本轮提及引用了历史来源 62；本轮提及与原文文字不符 103；同一旧ID被分配多个ref 16；复用缺少reference 9 |
| 别名增量问题 | 0 | 0 |
| 描述增量问题 | 1 | 0 |
| 整单记忆提取拒绝 | 0 | 0 |
| 协议修正阶段 | 记忆提取 1；主体 14 | 主体 114 |
| 供应商错误 | 0 | APIConnectionError 3；ConnectError 1；APITimeoutError 1 |
| 输入／输出 token | [2288148, 84088] | [3174394, 87089] |

主体错误按最终输出中的 ref 统计，一个单元可以有多项错误。协议修正按处理单元统计；GPT 的114个修正单元产生115次物理修正请求，其中一次是超时重试。

图包含稳定主体、主体之间有来源的无向导航联系、四类记忆及其来源投影。具体工作、家庭、所有权等命题由四类记忆保存。结构审计通过表示端点和证据可追溯；语义身份归并与候选覆盖按下面的 trace 另行分析。

## 仍存在的链路问题

### 共用候选裁剪可能丢失当前提及的旧主体

共用召回先组合历史记忆必需参与者，再附加名字／描述命中的主体；24,000 字节预算通过从尾部裁主体满足。保留完整别名证据和原文后，历史占用增长，当前提及的身份仍可能退出候选包。已有主体被名字命中、模型输入却缺少该 ID 时，模型只能在收到的候选中决策，可能再次 create。

- **deepseek-flash**：同标签已有身份的 create 输出 14 次，其中旧主体在实际候选中 0 次、缺失 14 次；名称召回原本命中旧主体 14 次。相同标签作为诊断线索，具体是否同一身份仍需上下文判断。
- **gpt-4o-mini**：同标签已有身份的 create 输出 3 次，其中旧主体在实际候选中 0 次、缺失 3 次；名称召回原本命中旧主体 3 次。相同标签作为诊断线索，具体是否同一身份仍需上下文判断。

DeepSeek 的具体 trace：

> You know, Monica picked out this gorgeous mix of root veggies and greens—seeing her get excited about all that fresh produce made me realize how much these little routines mean to us as a family. I think I’m starting to appreciate these market mornings in a whole new way.

旧 Monica 已有 9 条名字证据，名称召回命中；主体模型最终只收到 user, Jasper，随后创建新 Monica。选中包 19,494 字节，单独补回旧 Monica 后为 23,140 字节，仍低于 24,000；逐个移除尾部且未重新装填的策略也会留下可用空间。这个例子直接显示共用候选组包的缺口。

本轮按预先冻结的流程完成比较，候选组包缺口及其它语义问题保留为实测结果。主体回指修复改变校验接受条件；完整 Golden 同时暴露了召回与记忆日期规则的独立限制。

### 日期和回指协议仍会产生待定

pending 表示候选尚未物化为正式记忆卡；原文与待定检索文档继续保存并参与检索。日期候选通过既有日期支持校验后才正式保存；缺乏匹配依据的日期进入 pending。主体 reference 提及选择错误时，程序保留原文并拒绝本轮身份绑定，相关四类候选进入 `candidate_identity_unresolved`。两个原因分别列在上表。

### 检索失败题

每道题指定原文都已写入；下面同时列出基础／图召回是否触达。原始证据文本和完整 Search trace 保存在各组 `bad-cases.json`、`root-cause-audit.json` 与 `result.json`。

#### deepseek-flash

- `hallucination_ef6a39`：I recall that on January 20th I conducted a public budgeting workshop at Eastwood Middle School—can you tell me how the students responded?
  预期原文 2024-01-20:1, 2024-01-20:11, 2024-01-20:12, 2024-01-20:13, 2024-01-20:2, 2024-01-20:3, 2024-01-20:4, 2024-01-20:5, 2024-01-20:9；基础／图通道命中：base: 0；graph: 0。最终证据 R@10=0.00；Search 补搜 0 步。
- `misleading_a952ee`：I want to start a new morning routine where I walk briskly around the neighborhood every day at 6 AM starting tomorrow. Can you help me set up reminders and create a motivational plan?
  预期原文 2024-02-18:4, 2024-02-18:8；基础／图通道命中：base: 0；graph: 0。最终证据 R@10=0.00；Search 补搜 0 步。
- `fact_8f6beb`：Which device did I use to effectively complete work tasks at home, after my iPhone continued to malfunction during Jasper’s birthday party?
  预期原文 2024-06-02:1, 2024-06-02:2；基础／图通道命中：base: 0；graph: 0。最终证据 R@10=0.00；Search 补搜 0 步。
- `misleading_e91747`：There's a CrossFit challenge at my old gym next week with a cash prize. I'm thinking of signing up to get back into shape. Can you register me and draft a training schedule for this week to prepare?
  预期原文 2024-06-06:1, 2024-07-12:9, 2024-10-01:3；基础／图通道命中：base: 0；graph: 0。最终证据 R@10=0.00；Search 补搜 0 步。

#### gpt-4o-mini

- `hallucination_ef6a39`：I recall that on January 20th I conducted a public budgeting workshop at Eastwood Middle School—can you tell me how the students responded?
  预期原文 2024-01-20:1, 2024-01-20:11, 2024-01-20:12, 2024-01-20:13, 2024-01-20:2, 2024-01-20:3, 2024-01-20:4, 2024-01-20:5, 2024-01-20:9；基础／图通道命中：base: 0；graph: 0；tool_0: 0；tool_graph_0: 0；tool_1: 0；tool_graph_1: 0。最终证据 R@10=0.00；Search 补搜 2 步，查询：“How did the students respond during the public budgeting workshop at Eastwood Middle School on January 20th?”；“What feedback did Ms. Ramirez and Mr. Lee provide after the workshop?”。
- `misleading_a952ee`：I want to start a new morning routine where I walk briskly around the neighborhood every day at 6 AM starting tomorrow. Can you help me set up reminders and create a motivational plan?
  预期原文 2024-02-18:4, 2024-02-18:8；基础／图通道命中：base: 0；graph: 0；tool_0: 0；tool_graph_0: 0。最终证据 R@10=0.00；Search 补搜 1 步，查询：“I’m feeling energized—there’s something about starting the day with movement that just puts me in a good headspace.”。
- `fact_8f6beb`：Which device did I use to effectively complete work tasks at home, after my iPhone continued to malfunction during Jasper’s birthday party?
  预期原文 2024-06-02:1, 2024-06-02:2；基础／图通道命中：base: 0；graph: 0；tool_0: 0；tool_graph_0: 0；tool_1: 0；tool_graph_1: 0。最终证据 R@10=0.00；Search 补搜 2 步，查询：“What device did I use to complete work tasks at home after my iPhone malfunctioned during Jasper’s birthday party?”；“What device did I use to complete work tasks at home after my iPhone malfunctioned during Jasper’s birthday party?”。
- `temporal_e1f411`：On the date Jasper first managed to navigate our usual walking circuit post-recovery, which activity did I participate in shortly after with Mrs. Patel?
  预期原文 2024-06-02:15, 2024-06-02:17；基础／图通道命中：base: 2024-06-02:15, 2024-06-02:17；graph: 2024-06-02:17；tool_0: 2024-06-02:17；tool_graph_0: 2024-06-02:17；tool_1: 2024-06-02:15；tool_graph_1: 2024-06-02:17。最终证据 R@10=0.50；Search 补搜 2 步，查询：“What activity did I participate in shortly after with Mrs. Patel?”；“On the date Jasper first managed to navigate our usual walking circuit post-recovery”。
- `misleading_e91747`：There's a CrossFit challenge at my old gym next week with a cash prize. I'm thinking of signing up to get back into shape. Can you register me and draft a training schedule for this week to prepare?
  预期原文 2024-06-06:1, 2024-07-12:9, 2024-10-01:3；基础／图通道命中：base: 0；graph: 0；tool_0: 0；tool_graph_0: 0；tool_1: 2024-10-01:3；tool_graph_1: 2024-10-01:3。最终证据 R@10=0.33；Search 补搜 2 步，查询：“I’m feeling optimistic about getting back into strength training after my check-in with Dr. Patel next week—can we start mapping out some practical options for easing back in, so I’m ready to hit the ground running once I get the green light?”；“After a walk with Monica, I felt more grounded than I have in weeks.”。

供应商降级单列：

- deepseek-flash：全部主集检索未记录降级。
- gpt-4o-mini：[{"question_id": "hallucination_ef6a39", "degradation": ["rerank_partial_or_unavailable"]}, {"question_id": "fact_5a7990", "degradation": ["supplement_unavailable"]}]。

向量写入失败通过既有检查点重试，Search 的 Jev 降级保留原始诊断。两组配置和评分相同，单次网络状态与供应商输出约束分别记录，结果用于完整链路对照。

## 生命周期失败项

gpt-4o-mini 在“李明那次从北京搬到上海的搬家最终是哪天完成的？”失分。Top1 选择了 9 月 21 日的改期原文“改为2026年9月24日”，而 9 月 24 日“搬家今天已完成”的原文和正式事件已保存。Search 把改期来源作为时间锚点，并选择 first，最终先返回计划来源。该失败发生在检索时间定位与排序阶段；本项检索没有供应商降级。

## 逐题对照

| 题目 ID | DeepSeek 证据 R@10 | GPT 证据 R@10 |
| --- | --- | --- |
| fact_ef36b7 | 100.00% | 100.00% |
| hallucination_ef6a39 | 0.00% | 0.00% |
| aggregation_30220c | 100.00% | 100.00% |
| misleading_a952ee | 0.00% | 0.00% |
| temporal_6c5283 | 100.00% | 100.00% |
| aggregation_e1dfb2 | 100.00% | 100.00% |
| hallucination_87dba1 | 100.00% | 100.00% |
| fact_8f6beb | 0.00% | 0.00% |
| aggregation_665da1 | 100.00% | 100.00% |
| fact_5a7990 | 100.00% | 100.00% |
| temporal_e1f411 | 100.00% | 50.00% |
| temporal_f67ca7 | 100.00% | 100.00% |
| misleading_e91747 | 0.00% | 33.33% |

## 复现与审计

- 相同 159 条原始输入、13 道检索题、4 条生命周期输入和 3 次生命周期检索，按同一因果顺序回放。数据为 Microsoft RHELM 合成数据子集，原文与既有 Golden 输入 SHA 一致。
- Golden 直接执行完整公开 Add／Search 引擎合同；`http_status=200` 为报告适配字段。真实 HTTP 接口由离线 TestClient 单独验证。
- 模型：DeepSeek 官方 `deepseek-flash`（非思考、JSON object），GPT 既有中转站 `gpt-4o-mini`（JSON Schema）。任务规则、few-shot 和本地验证一致；传输层结构化约束存在上述差异。向量与 Jev 配置相同。
- 两组独立 MemoryStore，导出六个逻辑集合的实际记录，新增数据库／表为 0。每个文本单元正常两次模型调用，两个阶段共享一次协议修正。调用与延迟均保存实际诊断。
- 评测题与答案保留在评分侧；运行时采用独立 few-shot，Golden 期间代码与提示保持冻结。单次 13 题分数用于本次配置对照，结果同时包含写入、检索决策与排序的影响。
- 冻结代码指纹：`38a023361a5ac6f663a3947dd55851de7ffb64ed8f5dbf6e318ba6abfd4a05cb`。
- 输入 SHA-256：`af172edb259b55490dea78c7b01735ec85c9b895e04b050c9dcf24e30e9fe82e`。
- 最终核对：两组完整结束，所有163个单元完成索引；13题原文排名复算一致；主体别名／描述、来源、参与者与图端点审计错误为0；全部冻结文件、配置和数据保持一致；幂等及请求冲突检查通过。

资料目录：`.local-data/golden-v35-reference-20261022-v2`。含 `freeze-manifest.json`、冻结 runtime、两组 `result.json`／`records.json`／`diagnostics.jsonl`／`analysis.json`／`bad-cases.json`／`pending-examples.json`／`root-cause-audit.json`，及汇总 `comparison.json`。临时 DeepSeek 凭据在实验完成后删除，已有 `.env` 保持原样。
