# 独立数据库范围

状态：用户于 2026-10-12 明确允许本文件列出的 6 张表及离线数据库测试。独立主库已初始化，SQLite 持久化回归已通过。

| 表 | 内容 |
| --- | --- |
| sources | 完整原文、角色、消息时间、接收时间、所属请求与会话 |
| memories | relation／fact／preference／experience／event；主体关系追加修订，四类记忆按来源追加记录 |
| objects | 按用户隔离的具体人／动物／组织与团队身份、原文名称、来源别名与观察时间 |
| edges | mentions、supported_by、revises，绑定对应修订 |
| requests | 请求内容指纹、每个处理单元的候选、主体／联系计划、记忆计划、处置与索引检查点 |
| search_documents | 原文检索窗口、记忆修订文档与待定候选提示、来源 ID、向量 BLOB |

冷热共享以上 6 张表，热视图按来源时钟与程序预算计算。sqlite-vec 以序列化和距离函数运行；当前实现采用普通表中的向量 BLOB，因此这份范围为 6 张物理表。索引 edge_origin、edge_target 为表索引。

`SQLiteStore.initialize(..., allow_create_schema=True)` 是唯一建表入口，创建全新文件。`SQLiteStore` 构造和 HTTP 启动读取已有结构。已有文件保留原状。项目默认路径为 `.local-data/remi.sqlite3`。

TraceWeave 的代码、14 表数据库、旧实验与检查点保持原样。物理数据库测试使用 `REMI_TEST_SCHEMA_APPROVED=1` 显式启用，测试样例保存于 pytest 临时库。

## 首版初始化时的验证状态

- 主库：`.local-data/remi.sqlite3`，权限 600。
- 实际物理表恰好为以上 6 张；sqlite-vec 加载后数量保持为 6。
- 完整性检查：`PRAGMA integrity_check` 返回 `ok`。
- 主库 6 张表均为 0 行，测试样例与正式开发主库独立。
- 全套离线测试：65 passed，0 skipped；本轮真实模型调用 0 次。

V19 继续使用上述六表和 JSON schema_version=12。显式联系可在同一 relation 卡中追加 update 修订，保持端点和关系 ID；每版保存自己的原文摘录和来源，查询按知识时间选择可见修订。其他状态卡继续追加保存，事件沿用局部状态修改。主体与联系的增量计划和只读绑定仍保存在 requests JSON。

V22 沿用六张物理表与 JSON 记录字段，GraphObject.kind 限定 person／animal／organization。旧宽泛 entity 记录通过明确原文重放进入新链路；本轮使用新的临时测试库，历史实验库保持原状。


V26 使用同一六表和 schema_version=12。主体模型直接读取原文并生成身份及关系维护计划，普通记忆候选在身份固定后构造。新检查点版本为 G11-direct-subject-v26；本轮没有新增表或自动 schema 初始化。无效主体句柄只留在请求检查点和待定流程，持久主体／图边只接收验证后的身份。

V27 共用召回复用 requests JSON 的 shared_recall／recall_trace 检查点，主体、关系、事件和 fact 沿用既有六表。pipeline=G11-shared-recall-v27，schema_version=12。测试使用已授权临时六表和 MemoryStore；正式实验库保持原状。


V28：主体与记忆 JSON schema_version=13，pipeline=G11-memory-types-v28；物理表继续为既有六表。relation 要求两个主体端点及来源证据，四类普通记忆为 fact／preference／experience／event。旧版类型混用通过原文重放重新生成，打开旧库时明确提示重放并保留原库。测试仅使用已授权临时六表与 MemoryStore，历史实验库保持原状。


V29：pipeline=G11-direct-memory-v29，JSON schema_version=13，继续使用六表。requests 保存两次模型输出与程序 write_plan；四类记忆的有效提取结果直接追加，关系继续维护稳定 ID 和修订。四类记忆的去重、归并及事件局部修订退出当前写入链路。原文、待定索引、来源图与原子检查点继续保留。
