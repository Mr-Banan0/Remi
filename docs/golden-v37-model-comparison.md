# V37：关键词主体召回后的双模型 Golden

日期：2026-10-23。两组使用同一冻结实现 `G11-keyword-subject-recall-v37`，同时启动、独立存储。159 条主集原文按相同因果顺序写入；另有四条生命周期写入。完整执行 Add／Search，主集 13 题、生命周期 3 题。

本轮 GPT 的原文证据 R@10 为 **73.08%**，DeepSeek 为 **69.23%**，两组均9/13题全证据命中、生命周期3/3。DeepSeek用时13.32分钟，GPT用时39.60分钟。主体审查同时发现：GPT主集仅三个主体，明确参与者漏建；DeepSeek仍有机构归并和描述归属问题。检索指标和主体维护质量分别评价。

## 结果与 V35 对照

| 模型／版本 | 原文证据 R@1 | R@10 | 全证据命中题 | 生命周期原文 Top1 | 总耗时 | 主集每条写入均时 | 主体修正单元 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek V35 | 50.00% | 69.23% | 9/13 | 3/3 | 14.13 分 | 4.60 秒 | 14 |
| DeepSeek V37 | 50.00% | 69.23% | 9/13 | 3/3 | 13.32 分 | 4.30 秒 | 8 |
| GPT-4o-mini V35 | 42.31% | 67.95% | 8/13 | 2/3 | 48.94 分 | 16.95 秒 | 114 |
| GPT-4o-mini V37 | 46.15% | 73.08% | 9/13 | 3/3 | 39.60 分 | 13.55 秒 | 92 |

R@K 对每题指定的原始来源证据计分，再对 13 题取平均。全证据命中表示全部指定原文进入前十。此轮测量检索证据覆盖，最终自然语言回答准确率未计分。主集每条写入均时用所有 Add 批次耗时之和除以159，批次延迟分布单独保存在原始报告中。

前缀中的来源摘要与原文证据分别统计；summary_only_no_original 表示相关摘要已可见，而指定原文没有回传。当前评分保持既有可见原文标准。

V35 与 V37 之间同时包含 V36 的结构化修正、V37 的关键词候选、两条先前消息和精简身份材料；历史对照反映这些改动的整体结果。单次小样本分数和模型输出随机性共同构成比较边界。

## 实际写入、主体图与待定原因

| 指标 | DeepSeek | GPT-4o-mini |
| --- | ---: | ---: |
| 主集主体数 | 18 | 3 |
| 主集主体类型 | {"individual": 14, "organization": 4} | {"individual": 3} |
| 主集四类记忆数 | 401 | 208 |
| 主集四类分布 | {"fact": 95, "preference": 72, "event": 122, "experience": 112} | {"experience": 76, "event": 76, "preference": 24, "fact": 32} |
| 无向主体联系 | 27 | 3 |
| 记忆／来源投影边 | 929 | 442 |
| 全部163单元提取候选 | 476 | 256 |
| 全部待定候选 | 68 | 41 |
| 待定原因 | {"event_date_requires_source_support": 68} | {"statements_preserve_time_scope_in_content": 17, "event_date_requires_source_support": 16, "candidate_identity_unresolved": 8} |
| 最终身份校验拒绝ref | 2 | 66 |
| 有最终问题且被记忆候选引用的单元 | 0 | 6 |
| 主体／记忆提取修正单元 | {"subjects": 8} | {"subjects": 92} |
| 示例原句命中且引用原文未包含的记忆 | 0 | 1 |
| 供应商错误 | {} | {"APIConnectionError": 1} |
| 最终待定与错误原文 | 见 pending-examples.json／root-cause-audit.json | 见 pending-examples.json／root-cause-audit.json |

图的来源、端点和归属审计，与身份／内容语义分别验收。全部结构审计通过，表示记录可追溯；人工抽查仍发现下面列出的身份归并、描述归属和内容错误。待定原文和检索文档保留在实际快照中。

**GPT 主集实际只有 user、Monica、Jasper 三个主体。**Alicia Torres、Ms. Ramirez、Principal Carter 等明确参与者仍可出现在记忆正文中，图中缺少它们的身份节点和参与联系。候选待定减少与主体覆盖完整性分别评价；Alicia Torres 的通话例子提供了实际输出证据。

## 主体候选裁剪与重复创建

V37 先固定已有 user 加最多五个其他关键词主体，再装入历史记忆。两条先前消息处理近邻指代；主体模型只接收身份卡和少量原文依据，四类记忆模型继续读取共用快照中的历史。

| 诊断 | DeepSeek V35→V37 | GPT V35→V37 |
| --- | ---: | ---: |
| 与已有同标签身份重合的新增ID | 14→0 | 3→0 |

同标签诊断用于发现潜在重复，具体身份仍需上下文确认。该指标覆盖同名分裂，学校与社区中心被合并则属于另一类问题。主集每次最终候选ID均与召回时固定的ID一致。

本轮实际候选平均／最大数量：DeepSeek 2.24／6，GPT 1.77／3（含已有user）；两组被数量上限截断的单元均为0，先前消息均最多两条。GPT仍有两次在已有同标签身份可见时提出create，均因协议校验失败而未生成新ID。

## 检索 bad case：原始上下文与失败环节

### hallucination_ef6a39：错误日期与错误活动组合

证据R@10：DeepSeek 0.00%；GPT 0.00%。

题目声称1月20日在 Eastwood Middle School 举办预算工作坊。当天原文记录一家人去 Nashville Farmers’ Market，包含 Jasper 看蜂蜜摊、Monica 挑蔬菜等经历；指定九条原文用于纠正前提。

核对具体的 candidate_channels 与 Search decision。DeepSeek 在本轮把2024-01-20放入 action_time_expression，steps=[]，基础和图通道均未触达九条市场原文。检索返回的是其它日期的学校工作坊等记录。

- DeepSeek：缺失来源阶段 {'recall_miss': 9}；通道触达 {"base": [], "graph": []}；补搜0步；降级 []。
- GPT-4o-mini：缺失来源阶段 {'recall_miss': 9}；通道触达 {"base": [], "graph": [], "tool_0": [], "tool_graph_0": [], "tool_1": [], "tool_graph_1": []}；补搜2步；降级 []。
  实际补搜：[{"purpose": "history", "query": "What was the response of the students during the public budgeting workshop at Eastwood Middle School on January 20th?", "time_expression": "2024-01-20T00:00:00+00:00", "anchor_source_id": "src_fc20d390b52d29105784ba82", "time_basis": "event", "relation": "independent", "select": "first", "window_days": 0, "anchor_step": null}, {"purpose": "history", "query": "What was the response of the students during the public budgeting workshop at Eastwood Middle School on January 20th?", "time_expression": null, "anchor_source_id": null, "time_basis": "event", "relation": "independent", "select": "first", "window_days": 0, "anchor_step": 0}]

### misleading_a952ee：晨走计划未唤起旧伤

证据R@10：DeepSeek 0.00%；GPT 0.00%。

4月18日用户想从明天早上6点开始快走。2月18日原文说晨走扭伤脚踝、错过部分会议，并提到8th and Maple附近结冰滑倒。指定两条原文需要作为相关历史被检索到。

查看两条伤情来源的正式记忆与通道触达记录。DeepSeek 已保存相关正式记忆，基础／图召回均未命中，Search 没有补搜。

- DeepSeek：缺失来源阶段 {'recall_miss': 2}；通道触达 {"base": [], "graph": []}；补搜0步；降级 []。
- GPT-4o-mini：缺失来源阶段 {'recall_miss': 2}；通道触达 {"base": [], "graph": [], "tool_0": [], "tool_graph_0": []}；补搜1步；降级 []。
  实际补搜：[{"purpose": "history", "query": "What are some tips for keeping momentum going with a morning routine?", "time_expression": null, "anchor_source_id": "src_24c0a3a98492aa2687e273a3", "time_basis": "message", "relation": "independent", "select": "all", "window_days": 0, "anchor_step": null}]

### fact_8f6beb：手机故障线索与工作设备答案分处不同场景

证据R@10：DeepSeek 0.00%；GPT 50.00%。

4月14日原文说 iPhone 在 Jasper 聚会上反复冻结。答案在6月2日：先说在家为 Eastwood Middle 八年级学生线上授课，随后说 backup Dell XPS 13 和降噪耳机工作可靠。两段相隔数周；本轮答案证据为6月2日的第1、2条。

题目使用手机故障与聚会来定位，答案原文谈线上授课和备用电脑。DeepSeek 对两条答案来源均未召回，也未发起补搜。可定位到基础／图召回覆盖和后续检索决策；具体词法／向量排序的因果贡献继续保持未推断。

- DeepSeek：缺失来源阶段 {'recall_miss': 2}；通道触达 {"base": [], "graph": []}；补搜0步；降级 []。
- GPT-4o-mini：缺失来源阶段 {'recall_miss': 1}；通道触达 {"base": ["2024-06-02:2"], "graph": [], "tool_0": [], "tool_graph_0": [], "tool_1": [], "tool_graph_1": []}；补搜2步；降级 []。
  设备答案所在的6月2日第2条原文已经返回，Dell XPS 13可见；缺失项为第1条线上授课背景原文。该题得分反映两条证据的完整度。
  实际补搜：[{"purpose": "history", "query": "What device did I use to complete work tasks at home after my iPhone malfunctioned during Jasper’s birthday party?", "time_expression": null, "anchor_source_id": "src_b760c955a5e377c99b93b683", "time_basis": "message", "relation": "independent", "select": "first", "window_days": 0, "anchor_step": null}, {"purpose": "history", "query": "What device did I use to complete work tasks at home after my iPhone malfunctioned during Jasper’s birthday party?", "time_expression": null, "anchor_source_id": null, "time_basis": "message", "relation": "independent", "select": "first", "window_days": 0, "anchor_step": 0}]

### misleading_e91747：高强度训练的健康历史证据回传

证据R@10：DeepSeek 0.00%；GPT 0.00%。

用户想报名下周 CrossFit 比赛。相关历史分布在6月6日力量训练时腰部受伤、7月12日医生建议暂停户外活动并卧床、10月1日持拐杖慢走。指定三条原文用于提供既往限制背景。

原文均保留。DeepSeek 已把7月12日的卧床和暂停活动限制摘要放入返回前缀；三条指定原文没有进入基础／图通道，steps=[]。此题包含摘要已可见、原文证据未回传的情况，严格证据分数为0；6月6日和10月1日原文也未命中。当前身体状况与实际建议需要后续回答阶段结合时间语境处理。

- DeepSeek：缺失来源阶段 {'recall_miss': 2, 'summary_only_no_original': 1}；通道触达 {"base": [], "graph": []}；补搜0步；降级 []。
  已可见的相关摘要：[{"content": "Dr. Patel is recommending I stick to mostly bed rest until we can do an in-person evaluation.", "source_refs": ["2024-07-12:9"]}, {"content": "Dr. Patel was pretty concerned about how much my pain had ramped up.", "source_refs": ["2024-07-12:9"]}, {"content": "Dr. Patel told me to hold off on even the lightest outdoor activity for now.", "source_refs": ["2024-07-12:9"]}]
- GPT-4o-mini：缺失来源阶段 {'recall_miss': 3}；通道触达 {"base": [], "graph": [], "tool_0": [], "tool_graph_0": []}；补搜1步；降级 []。
  实际补搜：[{"purpose": "history", "query": "I’m feeling optimistic about getting back into strength training after my check-in with Dr. Patel next week.", "time_expression": null, "anchor_source_id": "src_f8c3afb369c82a9a8955b9c6", "time_basis": "message", "relation": "independent", "select": "first", "window_days": 0, "anchor_step": null}]

## 写入与主体语义 bad case

### DeepSeek：学生群体短语被挂到教师描述

来源：`rhelm:David_R._Ellis:2024-02-03:turns-11-11`，单元 `0`。

> This morning at Eastwood, Ms. Ramirez and I kicked off our financial literacy session with her 8th graders, diving right into some hands-on budgeting scenarios. The energy in the room was fantastic—students were quick to jump in with ideas and questions!

原文 her 8th graders 指她的八年级学生。模型将该短语写成 Ms. Ramirez 的主体描述。校验确认短语位于原文，语义归属未被程序识别。

主体输出中的关键选择及记忆处置保存在 `manual-bad-cases.json`，可与本轮原始 checkpoint 逐项核对。

### DeepSeek：学校与社区中心被归入同一主体

来源：`rhelm:David_R._Ellis:2024-05-12:turns-1-20`，单元 `1`。

> This morning, as we were walking, I started tracking our steps on my Fitbit, and Jasper suddenly got all excited after spotting a pair of cardinals—he convinced us to take a little detour through the park by Eastwood Community Center.

所选旧主体已有 Eastwood Middle School 名字，当前原文提到 Eastwood Community Center。来源没有给出二者同一身份或改名依据；模型以共享 Eastwood 名称复用并追加别名，程序文字和来源校验通过。

主体输出中的关键选择及记忆处置保存在 `manual-bad-cases.json`，可与本轮原始 checkpoint 逐项核对。

### GPT-4o-mini：明确的新参与者未提出主体

来源：`rhelm:David_R._Ellis:2024-01-01:turns-9-16`，单元 `4`。

> This morning, I had a really energizing Zoom call with Alicia Torres where we put the finishing touches on our upcoming teacher training workshop agenda. I’m especially excited about a new Q&A session we decided to add—want to hear how that idea came about?

原文明确与 Alicia Torres 通话，主体输出只有 @user。后续事件按已固定视图只引用 user；原文与事件内容保留 Alicia，图参与者缺少她。该次是新人维护输出遗漏，旧主体召回没有 Alicia 属于正常状态。

主体输出中的关键选择及记忆处置保存在 `manual-bad-cases.json`，可与本轮原始 checkpoint 逐项核对。

### GPT-4o-mini：当前提及字段失配导致依赖记忆待定

来源：`rhelm:David_R._Ellis:2024-01-01:turns-1-8`，单元 `7`。

> Let’s lay out the meal plan in a table—menu items, ingredients, portion sizes, and who prepped what—plus a quick rundown of the steps and a note about Jasper’s enthusiasm and his resolutions. Can you help format that for easy reference?

模型为 @user 填写 reference.mention=`I`，当前原文使用的是 `Let’s` 等表达；它还填入当前没有出现的 `Monica`。程序拒绝不符合当前原文的引用，依赖未确认ref的记忆进入 candidate_identity_unresolved。该例的用户身份选择与当前提及字段准确性分别审阅。

主体输出中的关键选择及记忆处置保存在 `manual-bad-cases.json`，可与本轮原始 checkpoint 逐项核对。

### GPT-4o-mini：教师反馈原文被写入交接经验示例

来源：`rhelm:David_R._Ellis:2024-01-01:turns-9-16`，单元 `5`。

> It actually came straight from recent feedback—teachers wanted more space to ask real-time questions, so Alicia and I agreed a dedicated Q&A would make the workshop more responsive and practical. I’m really optimistic it’ll boost engagement!

当前讨论教师希望增加问答时间。经验候选复制了 few-shot 的交接经验句，并引用当前原文。来源引用存在、结构与日期字段合法，内容缺乏原文支持；程序仍将其保存为经验。

实际保存的 experience：`After missed handoffs, I learned to confirm the next owner in writing.`。程序确认了来源和字段，内容与引文是否相符仍依赖提取模型的语义判断。

主体输出中的关键选择及记忆处置保存在 `manual-bad-cases.json`，可与本轮原始 checkpoint 逐项核对。

### DeepSeek：日期归一化结果与原文表达协议衔接

> Hey, I woke up at 6:30 this morning here in Nashville—honestly, I felt surprisingly refreshed, even though I’m still carrying a bit of that travel fatigue from December. It’s weird; I thought I’d be dragging, but instead, I felt this quiet energy, like maybe I’m ready for a reset.

事件候选：I woke up at 6:30 this morning in Nashville.；返回 time_expression=`2024-01-01 06:30`；来源消息时间 `2024-01-01T07:49:00+00:00`。

该例把原文中的时间表达归一化为日期时间，当前支持校验将其置为 event_date_requires_source_support。候选含义和字段协议分别审阅；这类待定应结合原文判断，统计量不能直接视为同数量的事实错误。当前写入校验将这些候选保存为待定，原文及待定检索文档继续可用。

## 生命周期检索

| 查询 | DeepSeek | GPT-4o-mini |
| --- | --- | --- |
| 李明那次从北京搬到上海的搬家最终是哪天完成的？ | 通过 | 通过 |
| 李明在2026年9月24日住在哪里？ | 通过 | 通过 |
| 李明现在住在哪里？ | 通过 | 通过 |

## 逐题分数

| 题目ID | DeepSeek | GPT-4o-mini |
| --- | ---: | ---: |
| fact_ef36b7 | 100.00% | 100.00% |
| hallucination_ef6a39 | 0.00% | 0.00% |
| aggregation_30220c | 100.00% | 100.00% |
| misleading_a952ee | 0.00% | 0.00% |
| temporal_6c5283 | 100.00% | 100.00% |
| aggregation_e1dfb2 | 100.00% | 100.00% |
| hallucination_87dba1 | 100.00% | 100.00% |
| fact_8f6beb | 0.00% | 50.00% |
| aggregation_665da1 | 100.00% | 100.00% |
| fact_5a7990 | 100.00% | 100.00% |
| temporal_e1f411 | 100.00% | 100.00% |
| temporal_f67ca7 | 100.00% | 100.00% |
| misleading_e91747 | 0.00% | 0.00% |

## 调用、降级与可复核材料

- DeepSeek：供应商物理调用 {"add_subjects": 163, "add_propose": 163, "embedding": 340, "add_subjects_repair": 8, "search_decision": 16, "rerank": 37}；错误 {}。
- DeepSeek：输入／输出token 1,750,488／80,242；主体阶段输入／输出 1,014,204／34,359。
- DeepSeek：主集检索降级 []。
- GPT-4o-mini：供应商物理调用 {"add_subjects": 163, "add_propose": 163, "embedding": 364, "add_subjects_repair": 92, "search_decision": 16, "rerank": 55}；错误 {"APIConnectionError": 1}。
- GPT-4o-mini：输入／输出token 2,267,900／83,249；主体阶段输入／输出 1,517,460／36,500。
- GPT-4o-mini：主集检索降级 []。

单次供应商调用均时：

| 阶段 | DeepSeek | GPT-4o-mini |
| --- | ---: | ---: |
| add_subjects | 1.26 秒 | 4.20 秒 |
| add_subjects_repair | 1.58 秒 | 4.63 秒 |
| add_propose | 1.61 秒 | 5.40 秒 |
| search_decision | 1.37 秒 | 5.33 秒 |
| embedding | 0.26 秒 | 0.31 秒 |
| rerank | 1.95 秒 | 2.00 秒 |

主体正常请求的实际输入token均值／最大值：DeepSeek 5894／6808; GPT-4o-mini 5711／6631。网络错误与输入过长错误按真实诊断分别统计。

两模型分别承担主体维护、四类记忆提取和Search决策；向量与Jev配置一致。DeepSeek沿用官方deepseek-flash非思考、JSON object；GPT沿用既有中转gpt-4o-mini、JSON Schema。两个文本传输约束差异单独保留。

Golden直接调用公开Add／Search引擎合同；报告中的http_status为适配字段，HTTP实际协议由绑定该版本的427项离线回归单独验证。真实实验使用独立MemoryStore，六逻辑集合均导出，新增物理数据库与表为0。

记录、图端点与来源审计为0错误；全部13题证据排名重新计算一致，全部163个单元完成索引。代码、prompt、few-shot、输入和评分在运行期间保持冻结。完整验证字段见comparison.json。

主体图与内容语义的人工例子用于暴露具体问题；本轮没有给全部主体、描述和记忆计算人工语义正确率。

运行时代码指纹：`2891019f45b48567e9b7b9ea8764fd170c13d84a1a9ccba4a023b18e455cd9ba`。
输入SHA-256：`af172edb259b55490dea78c7b01735ec85c9b895e04b050c9dcf24e30e9fe82e`。

材料目录：`.local-data/golden-v37-keyword-20261023-v1/`。包含冻结runtime、输入和manifest；两组result／records／diagnostics／analysis／bad-cases／pending-examples／root-cause-audit；manual-bad-cases.json和comparison.json。历史V35材料保留原状，当前工作区保持未提交。
