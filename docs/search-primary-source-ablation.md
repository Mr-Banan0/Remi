# 主要原文与辅助上下文的重排归属实验

目标：让 Jev 评价主要原文的贡献，辅助上下文用于理解指代、说话者和日期。主要原文能确认或补充上下文；辅助上下文中的独立事实继续由其自身来源承担证据价值。

独立样本包含存放位置、日程条件、偏好、取消状态，以及中文、西班牙语表达，每组交换卡片顺序。现有平铺卡片 3/12 次优先选择直接原文；增加主要来源 ID 和说明后 10/12；将主要原文与上下文分成 `original`、`context` 后 12/12。另一组独立指代、日期、确认与说话者测试为 12/12。样本及输出在 `/private/tmp/remi-primary-evidence-check.py`、`remi-primary-evidence-check.json`、`remi-separated-evidence-check.py`、`remi-separated-evidence-check.json`、`remi-separated-context-check.py`、`remi-separated-context-check.json`。

实验开关为 `scripts.replay_golden_search --primary-source-rerank`，与完整摘要、支持度混合模式互斥。`_source_blocks` 提取既有卡片构造步骤，生产卡片保持原样；实验使用来源块构造 Jev 视图，Planner、返回结果、时间核验项、召回候选和预算均保持原样。无额外模型调用。

对照使用 V76 查询边界、首次 V79 高分运行记录的 Planner 和时钟。先比较逐题候选 ID、RRF 与分数，再比较召回、MRR、生命周期和延迟；若有稳定收益再考虑生产采用。小样本成功本身不代表 Golden 已改善。
