# Remi

Remi 是独立的 G06 冷热统一图实验项目，与 TraceWeave 并列开发。

主项目目录为 `/Users/bananabig/Dev/agent-memory-leaderboard-eval/solutions/remi`。包和 CLI 均使用 `remi`，当前配置变量使用 `REMI_`。历史报告继续保留生成时的名称和代码指纹，目录迁移规则见 [改名记录](docs/rename-to-remi.md)。

完整原文作为证据底座；偏好、事实／条件、事件、经验共用一种有来源和修订的记忆结构。人物、项目、客户、文档等对象提供组织入口。热区是同一张图的有预算视图，冷区继续参与全局检索。

## 当前实现

- 文本模型：`gpt-4o-mini`，按需拆分 content、沉淀固定 create/update 的内容与时间，负责一次 Search 补搜决策。
- Add 判断：`jev-1.13.0` Choice 负责单／多目标分流、固定类别与逐项目标／操作，Noul 核对拆分内容支持、覆盖及必要上下文。
- 向量模型：`text-embedding-v4`，默认 1024 维，沿用 DashScope。
- 重排序：`jev-1.13.0`，对检索证据支持度和时间候选活动匹配进行排序判定。
- Jev 沿用已验证网关协议：`https://api.quickrouter.ai/v1/systemone`，请求使用 `state/questions`，响应读取 `answers` 中的 Choice 概率与 Noul。
- HTTP `/add`、`/search` 与现有本地评测请求结构一致。
- 物理数据库：6 张显式 SQLite 表；向量采用 sqlite-vec 序列化和余弦距离函数，BM25 在程序中计算。
- Add 同用户串行：单目标直接使用原文，通常两轮逻辑 Jev＋一次最终写入 GPT；多目标按需一次 content 拆分，再分类、召回、选操作和最终写入。keep/retract 跳过写入 GPT，生成步骤共享最多一次修正。Jev 每批最多 4 项，物理调用按批计数。GPT 暂态传输重试与业务修正分别记录。
- Search 始终跨冷热混合召回，最多一次 GPT 决策、两步补搜；关系读取采用程序内有界操作。

[当前架构与调用预算](docs/current-architecture.md) · [G06 Add 局部 diff](docs/architecture-changes/g06-route-content-split.md) · [G05 历史 Golden 结果](docs/golden-g05-results.md) · [数据库范围](docs/database-scope.md) · [验收记录](docs/verification.md)

## 本地安装

```bash
uv sync --extra dev
uv run pytest -q
```

项目拥有独立依赖环境、数据库与测试输出。运行只依赖本项目代码。原有环境中的供应商凭据可以通过显式路径只读加载，后续也可以使用本项目的私密 `.env`。

```bash
export REMI_ENV_FILE=/absolute/path/private.env
uv run remi check-config
```

`check-config` 仅显示模型名称和凭据是否存在。实际调用前设置 `LLM_API_KEY`、`DASHSCOPE_API_KEY`、`TYPESAFE_API_KEY` 或相应的 `*_API_KEY_FILE`。`.env.example` 提供端点和预算模板。GPT 采用固定、小型 JSON Schema，原文与旧记忆 ID 在输入数据中提供。

## 显式数据库初始化

首次初始化先确认 [6 张表的范围](docs/database-scope.md)。取得允许后执行：

```bash
uv run remi init-db --path .local-data/remi.sqlite3 --allow-create-schema
```

应用启动读取已有数据库。初始化遇到已有文件会保留它并返回错误。测试创建临时数据库同样需要明确允许：

```bash
REMI_TEST_SCHEMA_APPROVED=1 uv run pytest -q
```

## 启动 API

```bash
export REMI_DB_PATH=.local-data/remi.sqlite3
uv run remi serve --host 127.0.0.1 --port 8011
```

`/health` 检查进程；`/ready` 检查本地配置和已有数据库。供应商可用性需要单独授权的真实调用验证。设置 `MEMORY_API_KEY` 后 `/ready`、`/add`、`/search` 支持 Bearer、Token 或 `x-api-key`。

### Add

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

原文完整保存，用户消息支持结构化内容；助手消息提供解释上下文并参与原文检索。`timestamp` 为 Unix 毫秒；来源时钟缺失时保留未知。相同请求 ID 与相同内容幂等，冲突内容返回 409。

Add 保存逐阶段检查点，完成操作处置与索引后返回 200。内部无效项独立处置；Jev 分流／分类／选择不确定或失败时保留原文并记录原因。初始拆分／最终文本写入或向量供应商不可用时返回 503 并保留检查点，重发相同请求复用完成阶段。G05 已完成记录继续可读；未完成的 G05 判断检查点使用对应冻结代码恢复，G06 核对版本后执行当前链路。

### Search

```json
{"query": "那次远程授课使用了什么设备？", "user_id": "demo-user", "top_k": 10}
```

返回 `data[]`，每项保留 `id/content/score/created_at`。内容包含本次选择的有预算工作背景、原始证据与必要记忆信息。Search 交付证据，后续 Agent 生成最终回答。

`x-memory-reference-time` 设置 ISO 查询时钟；为已有评测调用方保留 `x-traceweave-reference-time` 别名。以后评测迁移到新名称并确认旧调用方退出后，可以删除该别名。

## 最小实验边界

当前实现保留：来源、独立记录、追加修订、明确对象关系、跨冷热检索、程序时间计算、有限补搜和 Jev 排序。

自动主题组、自动 combine、全量 Profile 独立筛选、自动经验归纳、额外语义校验模型调用暂时退出 G01。每条经验首先来自明确用户陈述。

当前图检索提供一层对象关系读取及来源回查。Search 对活动核对通过且有时间的有界候选执行 first/latest，并保留到最终选择。活动核对缺失时标明覆盖不确定，基础召回继续返回。计数题和复杂多跳题的完整覆盖属于真实数据验收内容。

## Golden 对照

`scripts.run_golden` 使用隔离的六表测试库、真实本地 HTTP 和现有供应商配置。默认只打印计划；执行需显式 `--execute --allow-create-schema`，使用全新输出目录。标准答案与证据标签仅在测试侧读取。

```bash
uv run python -m scripts.run_golden --dataset /absolute/path/dataset-v1.json --baseline /absolute/path/traceweave-result.json --output /absolute/path/new-run --env-file /absolute/path/private.env
```

评分器核对实际返回的原文摘录与输入消息，并验证 TraceWeave 历史基线的逐题计分一致性。比较记录输入哈希、代码指纹、Top K、字节数、延迟与物理调用；历史基线用于开发对照。

向量查询当前采用精确距离扫描，词法评分按用户检索文档计算。冷热视图控制上下文和关系读取规模，整体长程索引性能需要通过增长测试衡量。

## 完整 David 与断点续跑

`scripts.run_david` 重放本地完整 David：1486 回合、215 次 Add、86 道 Search。默认只打印计划；执行使用冻结代码、全新输出目录、现有私密配置以及 `--execute --allow-create-schema`。

```bash
uv run python -m scripts.run_david --data-dir /absolute/path/rhelm --golden-dataset /absolute/path/dataset-v1.json
```

运行目录的 `run.json` 提供 `resume_command`。恢复核对代码、数据和非敏感配置指纹，跳过已完成 Add 与已原子提交 Search，继续数据库中的未完成阶段。完成整轮后 resume 的模型调用为 0；未提交的在途请求可能重执行，报告标注这个成本边界。

动作文件、数据库检查点、物理供应商日志、HTTP 尝试日志分别持久保存。每次暂态 HTTP 请求最多尝试三次，耗尽预算后保留现场。单运行目录使用排他锁，结果与汇总使用原子保存。评分仍核对返回的原文摘录，13 道已知题和其余 73 道同人物题分别报告。

## 完整验证计划

1. 结构与故障回归：来源角色、用户隔离、命令局部处置、修订、时间依据、重试检查点、冷热稳定 ID、原文可达、预算与服务错误。
2. Add 质量回归：独立新增、同一记录更新、不同事件、撤回、经验来源、错误确认及缺失记忆；单独记录初始生成、修正和物理调用。
3. 冻结同一批记忆，分别测试普通混合召回、图读取、图加冷热视图三个版本。使用 `REMI_GRAPH_ENABLED` 和 `REMI_HOT_ENABLED` 控制消融。
4. Golden 用于已见故障回归；另取工作项目、同名人物、多语言、冷区细节、旧有效条件与新修订样例验证泛化。
5. 比较严格原文证据召回、完整命中、排名、上下文大小、Add/Search 延迟与成本；最终回答质量另行 judge。

结构性测试通过表示程序执行合同正确。内容支持、同一记忆身份及语义召回效果仍需真实模型数据验证。付费测试按用户批准的数据、供应商及范围单独执行。
