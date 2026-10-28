# Remi 0.2.0

Remi 是面向 Personal Agent 的长期记忆服务。当前 API、CLI、AML Runner 和离线测试统一使用 `remi.engine.MemoryEngine`，运行管线标识为 `remi-0.2.0`。

系统完整保存对话原文，以主体作为身份范围，以 fact、preference、experience、event 四类 Memory 维护可检索结论。每条 Memory 保留来源、修订和图连接；Search 从原文、当前记忆视图和版本路径中返回证据。

## 当前链路

Add 对每个有效用户消息按固定顺序执行：

1. 保存 Source，并从当前用户的主体名称和别名中召回候选。
2. 主体模型判断 person、animal、organization 的 create、reuse 或 pending；程序校验名称、别名、证据和同名歧义，并固定本轮主体范围。
3. 候选模型从当前原文提取 fact、preference、experience、event，保留参与主体、表达归属、条件、状态、时间表达和来源。
4. 程序优先召回固定主体范围内的旧 Memory；相关性不足时补充无主体 Memory，只属于其他主体的记录保持在范围外。
5. 维护模型为每个候选选择可选旧 Memory，并返回完整正文。有效目标追加修订，空目标或无效目标创建独立 Memory。
6. 程序计算字段差异，写入修订、来源边、主体边和检索索引，并与请求检查点原子提交。

正常链路调用三次文本模型：主体维护、候选提取、记忆定稿。主体和候选阶段各有一次结构修正预算；记忆目标失效时，程序使用已验证候选独立新增。未确认主体的内容保留为无主体 Memory，等待以后获得身份依据。

Search 按以下顺序执行：

1. 在可见 Source 和 Revision 上执行基础词法与向量召回。
2. Planner 判断 direct、verify_premise、action、temporal_join、current_state 查询模式，并生成最多两个证据目标。
3. 各证据目标独立召回；程序在同一主体和时间范围内补全必要角色，并按信号充分度决定是否宽化检索。
4. 任一版本或来源命中后，按逻辑 Memory 聚合，并沿 `revises` 解析查询时间点的有效版本。
5. 从高相关种子执行有界图扩展；canonical user 不作为全库枢纽。
6. 各通道使用 Memory 级 RRF 融合，Jev 根据候选对原问题的证据贡献终排；Jev 不完整时保留 RRF 顺序。

完整结构和调用预算见 [当前架构](docs/current-architecture.md)。

## 数据合同

- Subject：稳定 ID、首次确认名称、后续别名、`person | animal | organization` 类型和指代证据。
- Memory：`fact | preference | experience | event`，包含一条或多条 Revision。
- Source：完整原始消息及角色、会话和时间信息。
- Edge：`mentions`、`supported_by`、`revises`、`related`。
- Schema：`schema_version=13`。
- SQLite：只使用既有 `sources`、`memories`、`objects`、`edges`、`requests`、`search_documents` 六张表。

## 安装与配置

```bash
uv sync --extra dev
export REMI_ENV_FILE=/absolute/path/private.env
uv run remi check-config
```

`check-config` 输出 Remi 版本、pipeline、模型名称和凭据是否存在，不显示密钥。实际调用需要配置 `LLM_API_KEY`、`DASHSCOPE_API_KEY`、`TYPESAFE_API_KEY`，也可以使用对应的 `*_API_KEY_FILE`。`.env.example` 提供端点和预算模板。

## 数据库初始化

首次初始化前确认 [数据库范围](docs/database-scope.md)，取得建表允许后执行：

```bash
uv run remi init-db --path .local-data/remi.sqlite3 --allow-create-schema
```

应用启动只读取已有数据库。离线测试使用获准的临时六表库：

```bash
REMI_TEST_SCHEMA_APPROVED=1 uv run pytest -q
```

当前统一版本的离线结果为 **450 passed**。

## HTTP API

```bash
export REMI_DB_PATH=.local-data/remi.sqlite3
uv run remi serve --host 127.0.0.1 --port 8011
```

- `GET /health` 返回服务状态、版本和 pipeline。
- `GET /ready` 检查本地配置、已有数据库、版本和 pipeline。
- `POST /add` 写入消息并维护长期记忆。
- `POST /search` 返回与问题相关的原文证据和记忆视图。

Add 示例：

```json
{
  "request_id": "demo-1",
  "user_id": "demo-user",
  "session_id": "demo-session",
  "messages": [
    {"role": "user", "content": "今天在家远程授课，用了备用 Dell XPS 13。", "timestamp": 1727863200000}
  ]
}
```

Search 示例：

```json
{"query": "那次远程授课使用了什么设备？", "user_id": "demo-user", "top_k": 10}
```

`top_k` 接受 1–100。`x-memory-reference-time` 可以传入 ISO 查询时钟；`x-traceweave-reference-time` 是现有外部调用方仍在使用的接口别名，删除条件是这些调用方完成迁移。

## AML Full 模拟

`scripts.run_golden` 使用当前 `MemoryEngine`：Search 请求 `top_k=100`，Add 按最多 20 条消息或 2,000 个 `unicode_word_units_v1` 单元分块，并报告 R@1/3/5/10/100、返回数量、Answer 输入体积和延迟。执行需要全新输出目录、隔离六表测试库和显式参数：

```bash
uv run python -m scripts.run_golden \
  --dataset /absolute/path/dataset-v1.json \
  --output /absolute/path/new-run \
  --env-file /absolute/path/private.env \
  --execute --allow-create-schema
```

V56 是当前管线推广前最后一轮历史 Golden 基线：GPT-4o-mini R@10 82.05%、全证据 9/13、MRR 0.8487；DeepSeek Flash R@10 79.91%、全证据 9/13、MRR 0.7532。该结果用于历史比较，不代表 `remi-0.2.0` 已完成新的付费 Golden。详见 [V56 双模型 Golden](docs/golden-v56-model-comparison.md)。

## 文档

- [当前架构与调用预算](docs/current-architecture.md)
- [数据库范围](docs/database-scope.md)
- [验收记录](docs/verification.md)
- [V56 Planner 历史改造](docs/architecture-changes/v56-evidence-slot-planner.md)
- [V56 双模型历史 Golden](docs/golden-v56-model-comparison.md)
- [G10 Golden 历史对比](docs/golden-g10-results.md)
- [改名记录](docs/rename-to-remi.md)

`docs/architecture-changes/` 和带版本号的 Golden 报告是实验档案。运行时、用户接口和当前说明以 `Remi 0.2.0 / remi-0.2.0` 为准。
