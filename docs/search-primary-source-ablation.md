# 主要原文与辅助上下文的重排归属实验

目标：让 Jev 评价主要原文的贡献，辅助上下文用于理解指代、说话者和日期。主要原文能确认或补充上下文；辅助上下文中的独立事实继续由其自身来源承担证据价值。

独立样本包含存放位置、日程条件、偏好、取消状态，以及中文、西班牙语表达，每组交换卡片顺序。现有平铺卡片 3/12 次优先选择直接原文；增加主要来源 ID 和说明后 10/12；将主要原文与上下文分成 `original`、`context` 后 12/12。另一组独立指代、日期、确认与说话者测试为 12/12。样本及输出在 `/private/tmp/remi-primary-evidence-check.py`、`remi-primary-evidence-check.json`、`remi-separated-evidence-check.py`、`remi-separated-evidence-check.json`、`remi-separated-context-check.py`、`remi-separated-context-check.json`。

实验开关为 `scripts.replay_golden_search --primary-source-rerank`，与完整摘要、支持度混合模式互斥。`_source_blocks` 提取既有卡片构造步骤，生产卡片保持原样；实验使用来源块构造 Jev 视图，Planner、返回结果、时间核验项、召回候选和预算均保持原样。无额外模型调用。

对照使用 V76 查询边界、首次 V79 高分运行记录的 Planner 和时钟。先比较逐题候选 ID、RRF 与分数，再比较召回、MRR、生命周期和延迟；若有稳定收益再考虑生产采用。小样本成功本身不代表 Golden 已改善。

固定池回放已完成：`.local-data/search-v82-primary-v76-boundaries-20261102-r1/result.json`。16 项候选 ID 与 RRF 分数和对照完全一致；R@10 82.48%、MRR@10 88.46%、R@100 97.44%，平均回放耗时 6.02 秒。R@10 与一次相同固定计划的原文对照相同，低于首次高分的 90.17%。因此本表示方式继续保持实验状态。

额外离线检查五次已保存的重排输出：相同 Jev 分数改按 RRF 排序，一次小幅改善、两次下降、两次不变，MRR 均不变。记录在 `/private/tmp/remi-tie-audit.json`。现有证据不足以采用该同分规则。

## Combined with adjacent source context on the new V81 bank

`.local-data/search-v83-primary-v81-boundaries-20261102-r1/result.json` freezes the V81 full run's Planner and query clocks. Against the V83 adjacent-context replay, all 16 candidate pools, RRF scores and returned card contents (ignoring order) are identical. R@10 increased from 95.73% to 96.58%, MRR from 82.69% to 92.31%, R@100 stayed 97.44%, lifecycle 3/3. Mean replay latency was 6.10 seconds, P95 7.48; no request errors occurred.

The action constraint's first evidence rank improved from 4 to 1, and the actual contact record from 2 to 1. The device question stayed at rank 2; the false-premise question also stayed at rank 2. This is a ranking improvement at constant output content, but it is a single run. A fixed-plan repeat and live-plan replay are running before any production adoption.

Fixed-plan repeat 2 reproduced R@10 96.58% and MRR 92.31%. Live V81 replays returned R@10 92.74%, MRR 86.15% and 86.54%; a V76 live replay returned R@10 92.74%, MRR 85.58%. All five runs retained R@100 97.44% and lifecycle 3/3. The representation is promoted in V84; the temporary wrapper and flag are removed. Full fresh-ingestion validation remains outstanding. See `golden-optimization-v84.md` for costs and limits.
