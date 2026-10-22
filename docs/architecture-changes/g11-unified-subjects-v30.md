# V30：统一主体决策与独立别名校验

当前 pipeline：`G11-unified-subjects-v30`。主链路为共用召回 → 主体及关系模型 → 身份与别名校验 → 四类记忆提取 → 校验与原子保存。正常两次模型调用，共享最多一次协议修正。

## 修改边界

- 主体模型输出只有 subjects 和 connections。subjects 每项包含 ref、operation、target_id、name、kind、aliases、evidence，operation 为 create／reuse／pending。
- create 使用 null target_id，由程序创建稳定 ID。reuse 选择召回中当前用户可见的 ID，aliases 提供有来源的新名称。pending 使用 null target_id，保留未确认身份。
- 每个身份在 subjects 中出现一次；同一人的不同名称共用 ref；同名不同人可分别创建。模型通过身份与原文语境选择，程序检查引用、类型、用户归属及证据。
- 程序先验证身份，再逐条校验别名。有效身份可以保留，错误别名记录 subject_alias_issues 并排除，其他有效别名正常追加。已存在的名称复用现有记录，主体资料保持原样。
- 修正保留已接受的主体与关系，同时提供被拒绝的原始操作及错误原因。程序内部的身份映射继续供图绑定与事务冲突校验使用。
- 旧模型 bindings 数组、SubjectBindingDraft 和主体 update 操作退出。统一列表消除了跨数组互斥约束；reuse 加别名提议覆盖已有身份的维护。
- 沿用既有六表及 schema_version=13；pipeline 更新使检查点绑定当前合同。历史实验继续使用自己的冻结代码，旧输入可通过原文重放进入当前链路。

## 完成标准与验证边界

离线验证覆盖已有主体复用、新主体、别名追加与隔离失败、同名不同人、歧义待定、主体关系端点、有限修正、故障续跑、幂等和 SDK 合同。模型示例全部经过实际解码及物化校验。真实模型对身份与语义的判断以独立实测记录为准。

## 本轮结果

全套离线 319 项通过（15.59 秒），新增主体协议回归文件覆盖 19 项参数化用例。src/scripts 指纹为 `ec289d4aafc01289d10f0632e0309aa7e83f0c8738f7624a2e0c9a1f81c91aad`。英文示例已经过实际解码与物化。真实模型、few-shot 泄漏率和 Golden 分数等待独立实测；离线通过表示程序合同及故障隔离通过。详细记录见 [验证记录](../verification.md)。
