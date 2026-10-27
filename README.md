# Remi

Remi 当前在开发 G11 主体记忆原型：具体人物、动物、组织／团队的主体 ID、可搜索称呼、四类记忆修订与版本链检索。

当前实验为 **V55 Objective-Aware Jev Search**，沿用 V53 Append First 写入：维护主体后提取事实 fact、偏好 preference、经验 experience、事件 event；有效旧 Memory 目标追加修订，null、遗漏或非法目标创建独立 Memory。Search 先规划 direct、premise_check、constraint、temporal_anchor、answer_detail、latest_state 证据角色，各角色独立召回后按逻辑 Memory 执行 RRF。每条候选携带命中的角色及该分支实际查询，Jev 按其对原问题的贡献完成最终语义排序。角色分支只扩大候选范围；first／latest 时间运算在 Jev 验证活动后由程序执行。V55 已通过 600 项离线测试和双模型 Golden：GPT-4o-mini R@10 为 75.64%、全证据 9/13、任一证据 11/13、MRR 0.7846；DeepSeek Flash 为 65.38%、8/13、9/13、0.6923；两者生命周期 Top1 均为 3/3。完整结果见 [V55 双模型 Golden](docs/golden-v55-model-comparison.md)。

历史 G10 V5 Golden 的原文 R@10 为 74.36%，完整命中 8/13、全漏 2/13，主集逻辑 Add 平均 28.54 秒。该成绩对应历史冻结代码，详情见 [G10 Golden](docs/golden-g10-results.md)。

项目目录为 `solutions/remi`。包和 CLI 均使用 `remi`，当前配置变量使用 `REMI_`。历史报告继续保留生成时的名称和代码指纹，目录迁移规则见 [改名记录](docs/rename-to-remi.md)。

完整原文作为证据底座；主体提供身份入口，关系保存主体之间的联系，四类记忆保存事实、偏好、经验和事件。主体关系按稳定 ID 维护修订，四类记忆按来源追加记录。热区是同一张图的有预算视图，冷区继续参与全局检索。

## 当前实现

- 文本模型：`gpt-4o-mini`，维护主体、提取四类记忆、选择可选旧 Memory 目标，同时负责 Search 补搜决策。
- Add 写入：保存原文并维护主体，提取四类候选，在固定主体 scope 内召回旧记忆；有效目标追加修订，其他候选独立新增。
- 向量模型：`text-embedding-v4`，默认 1024 维，沿用 DashScope。
- 重排序：`jev-1.13.0`，结合原问题、命中的检索角色和分支查询，对证据贡献度及时间候选活动匹配进行判定。
- Jev 沿用已验证网关协议：`https://api.quickrouter.ai/v1/systemone`，请求使用 `state/questions`，响应读取 `answers` 中的 Noul。
- HTTP `/add`、`/search` 与现有本地评测请求结构一致。
- 物理数据库：6 张显式 SQLite 表；向量采用 sqlite-vec 序列化和余弦距离函数，BM25 在程序中计算。
- Add 同用户串行：每个有候选的用户文本单元依次调用主体、候选提取和目标选择，共三次；主体与候选提取各自保留协议修正，目标选择失败执行独立新增。图、索引和应用检查点一起提交。
- Search 跨冷热混合召回全部可见版本；一次 GPT 决策产生最多两个带证据角色的补搜。direct／answer_detail 在直接覆盖充分时可跳过，constraint、premise_check、latest_state 与时间步骤继续执行。各通道用逻辑 Memory 级 RRF 合并，Jev 使用每条候选的实际检索目标终排；Jev 不完整时沿用 RRF，Planner 不可用时执行一次有界 constraint 补搜。

[V55 双模型 Golden](docs/golden-v55-model-comparison.md) · [V54 双模型 Golden](docs/golden-v54-model-comparison.md) · [G10 Golden 三版对比](docs/golden-g10-results.md) · [G10 实现与独立验收](docs/g10-validation-results.md) · [当前架构与调用预算](docs/current-architecture.md) · [G10 简化范围与预算](docs/architecture-changes/g10-local-maintenance.md) · [G09 三版对比与图审计](docs/golden-g09-results.md) · [G08 Golden 实验与实际图](docs/golden-g08-results.md) · [G05 历史 Golden 结果](docs/golden-g05-results.md) · [数据库范围](docs/database-scope.md) · [验收记录](docs/verification.md)

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

Add 依次调用主体维护、四类候选提取和目标选择。程序校验类型、来源、主体引用、日期支持和目标范围；有效目标追加到同一 Memory，null 或无效目标创建独立 Memory。模型遗漏或输出格式错误时，程序使用候选正文独立新增。未确认主体的候选保存为无主体记忆。检查点保存召回、主体、候选、目标选择和应用状态；索引故障重试复用已完成输出。原文、记录、图和索引提交后返回 200。JSON 记录保持 schema_version=13，实验检查点为 G11-append-first-memory-v53，沿用已有六表。

### Search

```json
{"query": "那次远程授课使用了什么设备？", "user_id": "demo-user", "top_k": 10}
```

返回 `data[]`，每项保留 `id/content/score/created_at`。内容包含原始证据、命中的版本、查询时间点的当前版本及版本路径。Search 以 Memory 为聚合单位，以原始 Source 为回答证据。原始查询与各证据角色分别保留有界候选，RRF 只融合名次；主体、时间和状态继续作为范围与角色规则。图从原始查询及各角色的高相关种子扩展，canonical user 不作为全库枢纽。

`top_k` 接受 1–100，返回最多请求条数。`query` 接收完整非空字符串，检索使用完整题面；模型决策继续使用现有字节预算。接口校验继续采用当前协议与预算。

`x-memory-reference-time` 设置 ISO 查询时钟；为已有评测调用方保留 `x-traceweave-reference-time` 别名。以后评测迁移到新名称并确认旧调用方退出后，可以删除该别名。

## 最小实验边界

当前实现保留：来源、四类记忆追加记录、主体与关系维护、关系追加修订、跨冷热检索、程序时间计算、有限补搜和 Jev 排序。

记忆及关系以明确来源和上下文为依据。实体关系、历史修订和作用域共同支持检索；边际收益广度搜索保留为后续讨论项。

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

主体边界与设备／地点等内容分流见 [V22 说明](docs/architecture-changes/g11-subject-boundary-v22.md)。

主体维护使用 3 个身份与 7 个关系的英文原文 few-shot，每例直接给出维护 JSON，见 [V26](docs/architecture-changes/g11-direct-subject-v26.md)。候选驱动的历史版本及其冻结成绩见 [V25](docs/architecture-changes/g11-relation-fewshot-v25.md)、[V24](docs/architecture-changes/g11-subject-fewshot-v24.md) 和 [V23](docs/architecture-changes/g11-subject-fewshot-v23.md)。
