# V34 主体验证结果：个体与组织，名字与描述分离

2026-10-22。修改前工作区已提交为 `7698b686a68d3354106198fcd920253f9c8b6633`。本轮更新独立主体实验，生产 Add 的 src 维持 V32。当前实验主体统一为 individual／organization，aliases 保存名字，descriptions 保存原文描述及来源；正常每轮一次 gpt-4o-mini 主体调用。实验数据均为既有虚构开发用例与本轮合成跨轮用例。

## 结果

| 指标 | V33 | V34 修复版 |
| --- | ---: | ---: |
| 相同 28 组主体与连接完整通过 | 20/28 | 18/28 |
| 其中 20 组主体身份及可检索名字／描述覆盖 | 13/20 | 13/20 |
| 上述 20 组纯身份对齐，描述完整性另计 | 17/20 | 16/20 |
| 相同 10 组扩展回归完整通过 | 8/10 | 6/10 |
| 原 4 组连续维护 | 4/4 | 3/4 |
| 新增 4 组描述跨轮验证 | 未运行 | 1/4 |
| 全套离线回归 | 346 passed | 363 passed |

同口径对照在评测侧将 V33 的 person／animal 合并为 individual，并将旧样本要求的描述词从别名投影到 descriptions；主体 ID 与模型原决策保持原样。两版采用同一身份、可检索覆盖和无向连接检查。新字段位置与程序描述校验另计，28／10 的共同口径结果仍分别为 20→18、8→6。旧 4 组连续维护的输入保持相同，V34 的实验共享候选池新增描述与无向一层连接，因此这些数字反映整体实验方案。

这些样本已用于长期开发回归。扩展 10 组沿用既有样本；新增 4 组属于本轮方案验收。当前结果评估整体改动，类型合并的单独效果仍需单变量证据。本轮结果支持程序闭环可运行，语义验收仍未通过。

## 已确认工作的部分

- 当前用户自我介绍、别名补充、宠物别名、引用作者归属等常见输入本轮正确。宠物连续三轮使用名字 Tansy、描述 my dog、别名 Tan，保持同一主体 ID，名字与描述分别保存；新增描述闭环通过 1/4 中的这一组。
- 中英文两组同名动物本轮均创建两个 ID，并分别保存描述。该局部改善与其他身份回归一起统计。
- 两个同名 Esra 分别具有 nurse／baker 描述。后续 The nurse sent a message 正确复用护士的 ID；模型同时多输出另一个 Esra，程序以其缺少本轮身份锚点拒绝，因此整组仍失败。选对目标与完整协议通过分别记录。
- 程序验证来源所有权、来源可见时间、稳定 user、类型、名字归属、描述原文支持、ref 唯一性与连接端点；历史描述按来源保留。离线测试及真实输出重放均可核验。

## 失败与环节

| 问题 | 原文及实际输出 | 发生位置与程序表现 |
| --- | --- | --- |
| 明确同名新人误复用 | another person named Rory；另一个周淼，仍复用旧 ID | 主体模型身份决策错误；名字与 ID 合法，结构检查接受 |
| 同一组织改名拆分 | Hillside Circle → Daybreak Circle，以及桑榆→榆影，创建新组织并连接旧组织 | 主体模型身份分组错误；模型用连接表达同一身份，程序仍接受合法的两个 ID |
| 明确新组织待定 | Another Beacon Assembly ... independently，输出新提及 pending，另复用旧组织 | 主体模型未完成新身份创建 |
| 歧义猜身份 | 两个 Sam／Hamid 候选中选择一个旧 ID | 主体模型消歧错误；目录与名字合法只能证明候选存在，实际指代关系仍缺语义证明 |
| 设备越过边界 | The new router arrived today，创建 Router／individual | 主体模型范围判断错误；合法枚举和原文出现不足以验证实体类别 |
| 描述漏提或粒度不一致 | 宋言→阿言已复用，但画家未提；两位 volunteer 已分开但缺 volunteer 描述；芝麻／麻麻正确，description 保存整句 | 前两项是描述遗漏；芝麻的原文与“狗”信息已保存，当前精确标签覆盖失败。当前召回用完整描述匹配输入，短查询“狗”也不能稳定利用整句描述。这同时暴露描述粒度与匹配策略的衔接问题 |
| 描述归属错配 | Selene 是 designer，模型另给 Alder Cooperative 添加 designer 描述 | 主体模型属性归属错误；程序只校验词存在于来源，无法证明该词描述所选主体 |
| 无效或多余连接 | 改名时输出 [0,1]，实际只声明 ref 0；后续轮次重提只有历史来源的联系；出现 [0,0] | 模型引用／工作范围执行错误；程序拒绝未知 ref、自连接、缺本轮来源的连接 |
| 当前用户建立失败 | I work at Winter Guild，空目录却输出 @user／reuse／null；修正后仍失败 | 主体模型操作选择错误；程序拒绝复用空 ID，公司仍保存，用户与公司联系缺失 |
| 跨种类同名描述缺口 | cousin Vale 与 dog Vale 初轮建两个 ID，descriptions 均空；后续同时输出两者并重提历史边 | 身份初轮分开，但区分线索遗漏，后续模型范围与连接引证继续出错 |

程序最终输出重放与 8 组连续测试的实际 records 全部一致，详细审计位于 sequential-final-output-audit.json。步骤中的 errors 字段记录首轮触发修正的错误；最终审计另列修正后的实际错误，报告以最终结果为准。

## 来源编号修复与运行次数

首轮真实试验发现 source_catalog 的键保留原 ID，而正文 source.id 已转为 S1／S2；部分带历史上下文和跨轮请求的 Schema 与正文编号不一致。修复是在短编号转换后按转换后的 source.id 重建字典，任务 prompt 和 12 组 few-shot 保持原样。新增捕获真实 wire request 形状的离线回归，核对正文、Schema 与来源引用使用同一编号空间。

诊断轮保留在 `.local-data/subjects-v34-20261022-v1`：28 组 22/28、10 组 8/10、原跨轮 4/4、新跨轮 0/4，共 57 次请求。编号修复后重新冻结并全套运行，以上主表使用修复版结果，避免混用诊断轮的较高数字。

修复版冻结指纹：`d4551a568e803ca7b7d4f4e352eeec1abf15c123b695aceffe5f0ccddace88be`。实际 54 次正常主体调用、6 次协议修正，共 60 次成功请求，网络失败 0；两轮合计 117 次请求。四类记忆模型调用 0，Golden 0，真实验证使用 MemoryStore，物理数据库和新增表均为 0。离线回归使用此前获准的既有六表，363 passed、0 skipped，12.96 秒，第三方 Starlette 弃用提示 1 条。

初次启动 28 组命令被自动审批拒绝，理由是目标服务与外发数据范围待核实。检查确认服务为用户既有 gpt-4o-mini 中转站，28 条 payload 与仓库虚构测试完全相同；补充这些证据后，同一验证获准执行。没有改走其他外发路径。

## 实现与结果位置

- 当前实验：experiments/subject_identity.py、experiments/subject_connections.py。
- 入口：scripts/validate_subject_connections.py。
- 新验证：tests/test_individual_subjects_experiment.py；既有连接不变量沿用新合同。
- 原始结果与完整模型请求：`.local-data/subjects-v34-20261022-v2/`。
- 原始输入字节与 V33 完全相同，代码与测试的冻结清单均通过核验。V33 冻结结果保持原状。

本轮代码与报告保留为可审阅的未提交变更。生产 Add 的新主体合同、索引、四类记忆接入及持久化切换尚属实验边界之外；当前语义结果尚未达到切换主链路的验收标准。


## 2026-10-22：相同 V34 主体测试切换 DeepSeek

用户要求使用其提供的 DeepSeek API，快速运行相同测试。核对官方更新记录及 models API 后，选择最新 DeepSeek-V4.1-Flash-1017，调用名 deepseek-flash，官方端点 https://api.deepseek.com，非思考模式，temperature=0，输出上限6000。

| 验证项 | GPT-4o-mini V34 | DeepSeek 最新版，非思考 |
| --- | ---: | ---: |
| 同一28组完整检查 | 18/28 | 25/28 |
| 同一10组扩展完整检查 | 6/10 | 10/10 |
| 原4组跨轮维护 | 3/4 | 4/4 |
| 新增4组描述跨轮维护 | 1/4 | 3/4 |
| 38组直接样本的既有纯身份检查 | 30/38 | 38/38 |

DeepSeek 本轮正确创建另一个 Rory／周淼；组织改名复用原 ID 并补别名；宋言增加“画家”；Selene 的 designer／painter 描述保留在人身上，公司未被写入该职业。Sam／Hamid 歧义保持待定，router 没有越过主体边界。

原严格评分保留25/28。剩余3题均已正确区分预期主体并保存名字：volunteer 两人分别存 the first volunteer／the second volunteer；芝麻存“这只狗”；圆圆两猫分别存“左边的猫／右边的猫”。旧检查要求独立短标签 volunteer／狗／猫完全相等，完整短语因此不通过。现有描述召回也依赖完整短语匹配，粒度差异确实会影响短查询，后续分析应区分身份语义与描述检索合同。

新增跨轮未全过的 shared_description：模型先将护士和面包师两个 Esra 分开，后续 The nurse sent a message 正确选择护士 ID；程序要求当前引证中出现已知名字 Esra 或完整旧描述 a nurse named Esra，因此以 subject_identity_requires_source_anchor 拒绝。一次协议修正后仍选对相同 ID，校验仍拒绝，整组保持失败。该失败属于模型输出与程序锚点规则的衔接。

核验：38组直接样本的完整messages与GPT实际请求逐字一致，输入fixture字节相同，V34冻结代码／校验器／评分代码完全一致。API传输存在必要差异：GPT使用response_format=json_schema；DeepSeek官方支持json_object，因此适配器使用JSON模式并继续运行原本地严格校验。max_completion_tokens映射为max_tokens，thinking显式disabled。未新增提示、few-shot或按样本修改决策规则。

真实请求55次，其中正常54次、协议修正1次；网络失败0；全部finish_reason=stop，响应model=deepseek-flash。本轮平均请求耗时约1.30秒；并发与供应商不同，该耗时为本轮观测。主体、四类记忆、Golden的运行范围沿用本轮授权：仅主体专项，四类记忆模型调用0，Golden0，MemoryStore，新增物理数据库与表0。生产Add保持V32，Git未提交新变更。离线主体原代码保持363项已通过的冻结版本，新增传输适配器已验证消息保留、输出预算、JSON模式和非思考参数。

密钥只写入600权限的临时凭据文件用于鉴权，运行后删除。代码、模型请求与结果中均不包含密钥，既有GPT配置保持原样。

完整结果：.local-data/subjects-v34-deepseek-20261022-v1/analysis.json；原始输出在direct／holdout／sequential／extra；实际网络请求及供应商返回元数据在transport；messages一致性证据在message-equivalence.json；跨轮最终输出重放与实际records全部一致。

官方资料：[更新记录](https://api-docs.deepseek.com/updates/)、[JSON输出](https://api-docs.deepseek.com/guides/json_mode/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。
