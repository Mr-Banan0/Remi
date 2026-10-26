# V46 纯主体身份验证结果

日期：2026-10-26。实验 pipeline：`G11-identity-only-subjects-v46`。模型：`gpt-4o-mini`。代码指纹：`fba7282151b82e04e3b279d5bfda8e0e0745ffacc01d3cb981a52c60afab47de`。

## 协议与离线验证

完整离线回归为 610 passed、15 skipped。主体 Prompt、JSON Schema、修正协议、提交路径和测试入口均采用只有 `subjects` 的合同。主体提交不创建 endpoint_ids 图边。本轮沿用已有六个逻辑集合，没有创建数据库表。

## 真实模型结果

| 组别 | 完整通过 | 检查通过 |
| --- | ---: | ---: |
| 单轮 holdout | 8 / 10 | 67 / 70 |
| 常规跨轮 | 4 / 4 | 14 / 14 |
| 同名与描述边界 | 1 / 4 | 37 / 40 |
| 合计 | 13 / 18 | 118 / 124 |

共调用主体模型 28 次，输入 111,009 tokens，输出 2,400 tokens。普通记忆模型调用为 0，数据库创建数为 0。三组运行期间代码指纹保持不变。

## 未通过样本

1. 两个同名 Hamid 具有相同且不足以区分的依据时，模型直接复用了其中一个 ID；期望保持 pending。
2. `The new router arrived today.` 没有合格主体，模型创建了 `@user`；期望返回空 subjects。
3. 已有“表亲 Vale”和“狗 Vale”后，`My dog Vale needs a walk.` 返回 pending；描述已经足以选择狗 Vale。
4. 已有护士 Esra 和烘焙师 Esra 后，`The nurse sent a message.` 返回 pending；描述已经足以选择护士 Esra。
5. `My dog is sleeping.` 正确复用了 Tansy，但尝试把当前原文没有出现的 Tansy／Tan 再次作为别名追加。程序校验拦截，协议修正后仍保留错误别名。

常规的新建、复用、用户自称、别名、组织改名、引用归属、普通跨轮别名和两个明确同名新人均通过。剩余问题集中在同名候选的证据充分性判断、空主体输出和已有别名的增量纪律。

逐项输出位于 `.local-data/subjects-v46-identity-only-20261026-v2/`。
