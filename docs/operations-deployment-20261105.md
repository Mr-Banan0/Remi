# 2026-11-05 长期运维部署验收

## 已验证的部署

目标为腾讯云广州、AML 当前域名 api.bananagreatagain.com 对应的现有服务。源码发布目录为 `/opt/remi-test/releases/remi-ops-20261105`，`/opt/remi-test/current` 指向该目录。保留原发布目录及其依赖环境，systemd 通过明确的 `PYTHONPATH` 加载新源码。源码包 SHA-256：`673a377b0075df1726f80ada8bc72b46306ec6d1b88e48ec3003bb1f9f62503b`。

迁移前六表记录数为：sources 5、memories 5、objects 2、edges 7、requests 5、search_documents 10。先执行 SQLite 一致性备份，再恢复到独立文件，对恢复副本应用投影与 WAL 迁移；三份记录数一致、完整性检查均为 ok。实际切换前再次停止旧服务并备份，随后迁移原库，保留全部记录。

保留文件：

- `/opt/remi-test/backups/ops-preflight-20261105.sqlite3`
- `/opt/remi-test/backups/ops-restore-drill-20261105.sqlite3`
- `/opt/remi-test/backups/pre-ops-cutover-20261105.sqlite3`

新服务已启动；公网 `/health` 返回 200。服务器内携带现有凭据访问 `/ready`、`/metrics` 均为 200；公网未鉴权 `/metrics` 为 401。

隔离用户 `ops-smoke-20261105` 经公网执行一次 Add 和 Search：Add 200，8.26 秒；Search 200，19.97 秒，返回一条结果并命中 amber 阅读灯证据。这是部署冒烟验证，不作为 Golden 召回或延迟统计。

## 运维规则

按用户确认，业务记忆及备份全部保留。`remi-backup.timer` 每日服务器本地时间 03:00 启动（随机延迟至多 10 分钟）；`remi-monitor.timer` 每 15 分钟检查。首次手动触发的备份、监测服务均 Result=success、ExecMainStatus=0，随后两个 timer 均 active。读快照并发上限为 1，服务停止等待上限为 920 秒。

告警条件：可用空间低于 1 GiB、WAL 大于 512 MiB、备份缺失或超过 36 小时。当前通过 systemd 失败状态和日志记录告警；外部消息通知渠道尚未配置。服务开启无正文的请求数值日志，指标使用最近 1,024 次请求计算分位数，进程重启后计数重新积累。

## 本地回归

577 项离线测试通过。1,000 条合成长期记忆，四个并发调用者、读快照额度 1，连续 200 次检索全部与预先串行结果一致，失败数 0。总时长 20.66 秒，吞吐 9.68 请求/秒，含串行预热的 P95 为 0.459 秒，峰值 RSS 约 90.1 MiB。外部模型使用固定模拟实现，因此这些数字衡量本地排队与执行能力。

原轻量投影的十万条规模、历史查询等价验证见 memory-projection-performance.md。当前两轮在线 Golden 结果由独立报告记录；本文件的部署验证与本地压力数据不替代在线质量结论。
