# 独立数据库范围

状态：用户于 2026-10-12 明确允许本文件列出的 6 张表及离线数据库测试。独立主库已初始化，SQLite 持久化回归已通过。

| 表 | 内容 |
| --- | --- |
| sources | 完整原文、角色、消息时间、接收时间、所属请求与会话 |
| memories | relation/event 独立记忆；修订保存在同一记录的 revisions 中 |
| objects | 按用户隔离的统一主体身份、原文名称、来源别名与观察时间 |
| edges | mentions、supported_by、revises，绑定对应修订 |
| requests | 请求内容指纹、每个处理单元的命令与处置、索引检查点 |
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
