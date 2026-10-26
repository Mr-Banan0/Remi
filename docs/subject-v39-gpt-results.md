# GPT 主体维护：V38 与 V39 三轮对照及根因

日期：2026-10-23。本轮质量门槛未通过，活跃实验已恢复 `G11-subject-prompt-v38-r4`；生产 src 维持 V32。V39 两版代码、测试和全部结果保存在冻结目录，原有未提交工作保留。本轮没有 Git commit、持久表结构新增、DeepSeek 调用或 Golden 重跑；SQLite 离线验证沿用此前获准的临时六表。

## 结果与取舍

| 版本 | 46组旧回归（三轮） | 均值 | 12组补充（三轮） | 协议修正（三轮合计） |
| --- | --- | ---: | --- | ---: |
| V38 基线 | 32、35、36 | 34.33/46 | 7、6、8 | 19 |
| V39-R1：精简输出＋当前提及 | 29、30、28 | 29/46 | 8、8、8 | 19 |
| V39-R2：操作分支Schema | 32、32、32 | 32/46 | 8、8、7 | 11 |

旧分数是身份、名字／描述、联系和协议检查全部通过的组数。该分数继续原样报告，分项审计与人工判断并列。两批样本都作为已查看的回归资料；三轮重复用于观察同配置波动，本轮分数不能直接代表最终数据集准确率。

R2相对基线输入token减少18.0%，输出token减少38.7%，协议修正19→11。实际API累计耗时992.04→827.19秒；该耗时包含供应商服务波动，属于这次运行的观测值。质量仍低于基线，而且出现基本用户身份回退，因此恢复现有入口并在主体阶段收口。后续记忆／检索及Golden验证的质量前置条件未满足。

## 实际实施内容

主体输出从完整持久字段改为当前身份决策和新增内容：`ref, mention, operation, target_id, label, aliases, descriptions`，联系只输出subject_refs。程序按所选ID补名称、类型、当前source_id和已有历史依据；同一次模型调用继续覆盖主体与联系，后续四类记忆提取保持一次调用。

当前mention覆盖create／reuse／pending，保留原文中影响身份判断的限定信息。已有B来源由程序按目标主体的有效依据稳定选择，并记录全部依据、选择规则及用途；它是历史出处审计，身份等同仍由模型决定。

R1把label与operation的条件留给prompt和程序校验；R2将create／pending的非空label及空target、reuse的空label及候选target写入anyOf Schema分支。普通prompt、修正prompt、9组示例、编译／校验／修正函数在R1和R2之间核对一致。其他变更包括pipeline版本及诊断、测试元数据。

## 分项审计补充

以下是统一审计器在显式标注范围内，对末次原始模型回复给出的受影响用例×轮次数。每版174个用例轮次，维度可以重叠；基线一例供应商无回复单独排除，未裁定语义保留manual_review。这些计数表示诊断影响范围，semantic_identity_accuracy继续为null。

| 分项 | V38 | V39-R2 |
| --- | ---: | ---: |
| 身份操作／目标选择 | 10 | 6 |
| 当前主体覆盖 | 34 | 46 |
| 当前原文引用 | 7 | 23 |
| 用户角色归属 | 9 | 8 |
| 描述 | 9 | 17 |
| 联系 | 20 | 18 |
| 操作条件字段 | 1 | 0 |

身份操作诊断减少，同时当前覆盖和原文引用问题增加。分项结果支持保留局部收益、恢复稳定入口的取舍，质量回退由原总分和独立分项共同判断。R1中无合法label而无法对齐的13项期望身份另记manual_review_protocol_invalid，避免把协议不可评分直接记作语义遗漏。

## 1．核心病例的候选已提供，错误发生在身份维护

以下例子的旧主体均已经进入真实输入，包括名称、身份依据和历史原文。它们具备候选可用条件，当前trace支持把问题定位到维护决策。

### 同一宠物被重复创建：Pip／Pippy

当前原文：`My rabbit Pip is also called Pippy. Pippy lives with my cousin Leda.`

候选包含旧Pip和Leda。V38三轮都create Pip，生成同一宠物的重复身份。R1和R2三轮都选择旧Pip的E2并补Pippy别名，均通过原检查。这是实际旧ID复用改善。当前完整用户覆盖还需单列：部分输出遗漏My所指的用户，旧通过分数没有覆盖这个缺口。

### 独立新组织被并入旧组织：Beacon Assembly

当前原文：`Another Beacon Assembly was founded independently of the one I mentioned earlier.`

V38和R1三轮都reuse旧组织；片段、ID和B出处可以合法，程序仍接受了错误的身份等同。R2三轮都create新组织并通过原检查。R2与R1的主体业务差异仅Schema约束，当前观测支持这个样本改善；它不足以证明字段精简普遍提升语义判断。

### 明确新人被待定：Rory／周淼

Rory原文：`I met another person named Rory today.` V38本次仅一轮错误pending，另外两轮create正确。它具有波动，旧单次失败不能当作稳定结果。R1三轮新人都create，但用户行三轮因label=null被拒。R2三轮均通过。

周淼原文：`今天我认识了另一个周淼。` V38三轮均通过。R2创建新人正确，同时把历史“周淼是我以前的老师”再作为当前mention返回、重报历史描述、连接新旧周淼，并遗漏当前“我”。历史老师被程序拒绝；这是当前／历史范围混淆、用户遗漏和无依据联系，两个周淼没有被合并。

## 2．字段数量减少仍可能增加条件负担

R1所有成功回复都能解析为JSON，形状符合发送的Schema；它仍出现create／pending的label=null、reuse的label非空或target为空。三轮最后回复受条件违约影响的来源数为4、6、6，同一来源可能包含多条错误。

这些行有ref、mention和明确operation，缺少必需label时无法把它们直接评为“语义遗漏”；统一审计保留wire错误并标记manual_review_protocol_invalid。

R2的209份成功回复逐份核对，JSON解析错误、操作分支违约、Schema拒绝和编译器异常均为0。这个结果证明本次机械契约约束改善，主体身份语义继续单独验收。

## 3．正确身份因原文摘录改写被拒

组织改名原文：

```text
Hillside Circle is now called Daybreak Circle. It is the same organization.
```

R2三轮均正确选择已有E1，也正确新增Daybreak Circle；mention返回：

```text
Hillside Circle is now called Daybreak Circle; it is the same organization.
```

模型把`. It`改为`; it`，程序要求当前原文连续片段，整条身份无法绑定。旧V38使用短mention `Hillside Circle`，三轮均通过。此处身份决策与来源摘录是两个不同判断；本轮增加长身份片段的要求提高了逐字复制负担。

这是正确ID被拒的确定路径。依赖该ref的后续记忆候选会进入candidate_identity_unresolved；原文索引仍保存，最终检索损失需完整评测实测，本轮没有把程序待定直接等同最终漏召回。

## 4．错误身份也能通过全部机械校验

自我介绍原文：`My name is Nessa and friends call me Ness.`

V38三轮都得到@user＋Nessa/Ness别名。R2三轮中只有一轮保持正确，另外两轮返回普通Nessa和昵称Ness。名称、类型、来源、当前mention均合法，程序保存is_user=false。

这是规范用户身份归属的真实回退，当前Schema及原文校验无法从合法字段推断正确的身份语义。它也是本轮恢复基线的重要原因。

两个无法区分的Hamid中直接选一个已有ID，也能合法通过程序；这属于错误消歧。逐字mention和合法B来源都只能提供可审计材料，身份是否相同需要独立判断。

## 5．历史材料既帮助身份对齐，也可能泄漏到当前写入

Lark样本正确复用旧ID并补LK；当前原文只写bird，模型新增pet bird来自历史，因此只有描述被拒，身份和别名仍接受。

Selene跨轮只有人物姓名或职业，旧基线三轮均把历史公司带入当前并虚构mention；修正仍未稳定撤回。该问题位于模型识别“哪些主体本轮实际被提及”的边界。

这些病例应分别检查当前提及覆盖和新增内容来源。程序已支持有效身份／别名／描述分开接受，把描述错误统称身份识别失败会掩盖真实改进位置。

## 6．旧评分存在身份和描述耦合

宋言复用旧ID、补阿言别名一直正确，V38漏画家描述，旧identity_and_names和groups_remain_distinct连带失败。R2三轮补出画家并通过，主要是描述覆盖改善。

两个志愿者或两只同名猫已经保存为两个独立ID，描述使用完整短语时，旧评分要求独立volunteer／猫词条，仍可能报告身份分组失败。身份分组、描述可检索性和联系依据分别审计；身份比较产生的无向边还存在语义误连，端点合法校验无法覆盖。

## 7．实验编译及修正的程序验证

开发V39原型时发现并修复了新增编译层的确定性问题：顶层错误在编译后被隐藏；错误按旧数组位置施加到恢复后的主体／联系；缺少新增数组误拒有效身份；非法ref类型崩溃；修正上一assistant消息补入模型实际未返回的值。编译问题、原始wire和最终有效结果现在分别留痕。

这些修复属于V39原型自身的编译／修正层，不能据此宣称V38存在相同程序缺陷。冻结候选完整离线478项通过。质量门槛仍取真实GPT身份、覆盖和回归表现。

## 数据、重试与复核

每版三轮，每轮46组旧回归（54次主体来源）及12组补充。模型参数gpt-4o-mini、temperature=0、max_completion_tokens=6000、timeout=45，每轮workers=2。正常Add预算维持两次模型调用。

V38有3次网络异常，其中第二轮blind-role-reference最终无回复；R1一次网络异常后恢复；R2两次均恢复。网络失败与模型语义、JSON格式和协议修正分别计数。R2响应自报gpt-4o-mini，并记录response ID和system_fingerprint；旧版本没有采集这些遥测，保留为缺失。

显式审计标注SHA256：`8767a3639795e47d8d331e30b46b3abb60ed39dd46e63ca201127e99105c3c49`。它在读取V39结果前冻结；运行时代码只读取原文与用户记忆。统一审计保持semantic_identity_accuracy=null，未能唯一语义裁定的行进入manual_review；finding条目数与受影响来源／用例数分别记录。

## 当前可用产物

- 活跃实验入口恢复V38；回退清单：`.local-data/subjects-v39-gpt-20261023-v1/rollback.json`。
- 各版本及成本：同目录`score-comparison.json`。
- 九轮逐例对照：同目录`case-matrix.json`，完整trace在`baseline-round-*`、`candidate-round-*`、`candidate-r2-round-*`。
- 原始请求、Schema、回复、usage和重试：各轮`transport/`。
- 统一原始决策审计：同目录`audit/final-v2/`，包含首次和修正后两层。
- 原型代码与测试：`candidate-runtime/`、`candidate-r2-runtime/`；旧基线：`baseline-runtime/`。
- 主动保留的开发工具：`scripts/run_subject_protocol_comparison.py`、`scripts/audit_subject_trace.py`及其专项测试。

本轮已完成实现、离线验证、九轮真实GPT对照和逐例诊断。精简协议取得机械约束及token收益，主体质量门槛未通过；现有运行入口继续使用V38。

最终复核：冻结V39-R2完整离线478 passed、0 skipped（13.68秒）；回退后的V38＋26项审计检查为459 passed、0 skipped（12.86秒）。三个runtime哈希、九轮输入哈希、冻结审计标注、生产源码及活跃实验恢复均核对通过，Git diff格式检查通过。原始648次物理请求的本机trace保留；密钥模式扫描0项。完整复核见实验目录`verification.json`。
