# V50：候选提取与记忆维护闭环

日期：2026-10-26。实验 pipeline：`G11-memory-maintenance-v50`。

## 目标与边界

V50 在 V49 主体结构上完成记忆维护闭环。主体仍由稳定 ID、本名、别名和 person／animal／organization 类型组成。事实、偏好、经验和事件继续使用现有 memories、revisions 与 edges 存储。实现沿用 sources、memories、objects、edges、requests、search_documents 六个逻辑集合。

正常 Add 使用三次模型调用：

1. 主体模型确认本轮人、动物和组织，程序固定主体 scope。
2. 候选模型从当前原文提取 fact、preference、experience、event 候选。
3. 维护模型根据候选和 scope 内召回结果选择 create、update 或 pending，并返回最终有效视图。

程序负责候选召回、无主体补搜、历史卡片压缩、字段差异、证据追加、同轮连接解析和原子提交。

## 召回

主体 scope 确认后，程序只召回以下记录：

- 与至少一个确认主体相连的记忆；
- 置信度不足时补充无主体记忆。

只属于其他主体的记忆保持在本轮范围外。召回查询优先使用候选内容，并结合当前原文和一条会话上下文。主体范围内最高 BM25 分数与完整允许池最高分的比值低于 0.80 时，程序触发无主体补搜；两个集合统一排序后最多提供六张卡。

每张卡始终保留当前有效视图。模型输入最多展示三条历史来源，每条来源最多 1,600 字节；存储层保留完整来源与全部修订。来源继续缩减后仍超出总预算时，程序才跳过该卡。

## 维护协议

每项使用稳定的 `C` 引用。模型操作收敛为：

| 操作 | 含义 |
| --- | --- |
| create | 当前候选是独立的新记忆，target_id 为 null |
| update | 当前候选承接一条同类型的已召回记忆，返回完整最终视图 |
| pending | 能确认存在待维护内容，但身份或旧记录目标无法安全确定 |

时间字段分别返回 `retain / set / clear`。参与者返回 `retain / add / remove / replace`。连接分为已有记忆新增、同轮候选新增和已有连接移除。

`changed_fields`、`change_quote` 和模型侧 `keep` 已退出协议。模型只表达一次最终决定，程序比较旧视图和新视图：

- 字段或连接发生变化时生成 `update` 修订；
- 有效视图相同时生成 `support` 修订，并追加当前来源；
- 新候选生成 `create` 修订。

support 修订保留相同正文、结构字段和记忆 ID。修订回放累积证据，同时保持语义时间线不重复正文。每次支持来源的表达方式记录在 maintenance_patch 的 evidence_mode 中。

## 同轮连接与处置结果

维护模型可以在 `links.add_current` 中引用本轮其他 `C` 项。程序先为所有可落库项计算正式记忆 ID，再解析同轮引用。待定或校验失败目标会使依赖项进入待定，事务中不会写入半条连接。

每个模型返回项最终获得以下处置之一：

- created
- updated
- supported
- pending
- validation_failed

pending 继续进入既有待定检索文档。validation_failed 保留在 requests 检查点，记录候选引用、数组位置和错误码。待定恢复同时支持新的 `C` 引用和历史索引。

## 完成标准与验证

完成标准为：更新维持原记忆 ID；重复确认追加来源；长期证据不会挤掉当前视图；同轮新事件可以原子连边；其他主体的记忆不会越界；每个返回项都有处置结果；正常链路使用主体、候选、维护三次模型调用。

离线验证结果：622 passed、15 skipped。专项测试覆盖 support 修订、50 条历史来源卡片、同轮新事件连接、validation_failed 处置，以及候选 C 引用到维护 prompt 的 P 句柄投影。字节码编译和 `git diff --check` 通过。
