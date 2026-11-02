# 相邻原文上下文实验

检验记忆细分后，单条主要原文缺少邻近解释是否限制检索。实验在同一 owner、session_id、request_id 内，从主要来源 ordinal 前后各取最多两条消息，并保留原有引用。窗口规则与语言、问题和数据集无关。已有数据库和记忆图保持原样。

使用现有证据字节预算与截取方式，主要来源仍在首位。Planner 使用原有卡片；最终 Jev 卡片和返回证据使用扩展后的来源集合。查询边界快照限定当时已写入的数据。这个实验会改变可见原文，因此需要同时报告 R@10、MRR、R@100、完整 facet 引文覆盖和返回字节数，避免把体积增长误当作排序能力提升。

开关：`scripts.replay_golden_search --adjacent-source-context`，与其他重排表示实验互斥。测试覆盖用户、会话、请求、邻接位置、主要来源和字节预算边界，以及文档不被修改、Planner 输入保持不变。生产默认路径不变。

第一轮使用 V76 查询快照与首次 V79 Planner 记录，排除重新规划和写入的随机性。只有输出正确性和成本都可接受，且收益可复现时，才考虑进入统一生产版本。

## Repeated results

| Run | Planner | R@10 | MRR@10 | R@100 | Lifecycle |
|---|---|---:|---:|---:|---:|
| adjacent-v76-boundaries-r1 | Frozen | 96.58% | 91.03% | 97.44% | 3/3 |
| adjacent-v76-boundaries-r2 | Frozen | 96.58% | 94.87% | 97.44% | 3/3 |
| adjacent-live-v76-r1 | Live | 92.74% | 85.38% | 97.44% | 3/3 |

Full directory names have prefix `.local-data/search-v82-` and suffix `-20261102-r1` / `-20261102-r2`. All three runs completed without service errors. Mean Search replay latency was 6.32, 6.19 and 11.91 seconds respectively; frozen runs omit Planner calls.

The first adjacent run returned 47,572 bytes at ten results on average, compared with 37,019 for the fixed-plan source-only repeat (+28.5%). Exact facet quotation coverage at ten results increased from 82.05% to 97.44%. At five results, adjacent context used 23,602 bytes with 93.59% facet coverage. The full-result facet coverage remained 97.44%. Audit: `/private/tmp/remi-adjacent-evidence-audit.json`.

Live planning still generated reminder and motivational advice queries for an action request; the first gold source then ranked tenth. MRR 85.38% remains below the historical 85.58% target. A combined private-history task prefix and adjacent-context run is being evaluated; stable superiority is not established.

Historical source visibility now also bounds adjacent messages. The completed snapshot runs use query clocks later than all their source dates, so this added boundary does not change their comparison. An independent test excludes a future-dated neighbor while retaining visible neighbors.
