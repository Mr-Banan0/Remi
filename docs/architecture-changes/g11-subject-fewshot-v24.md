# V24：三个精简英文主体示例

主体维护示例从 10 组收敛为 3 组，全部使用英文。SUBJECT_PROMPT 正文、提候选和记忆阶段提示内容、生产逻辑与输出合同保持。pipeline 更新为 G11-subject-fewshot-v24，绑定当前提示内容。

| Example | Source and context | Expected decision |
| --- | --- | --- |
| New subject, two names | “Mira is also called M.” No stored identity. | Create one identity for refs 0 and 1. |
| Existing subject, new name | “The same Mira is also called M.” Stored E1 is Mira. | Update E1 with refs 0 and 1. |
| Different subject, same name | “I met a different Mira today.” Stored E1 is the neighbor Mira. | Create a new identity despite the recalled name match. |

每组输入继续包含候选 ref、kind、aliases、召回目录和 choices；输出继续使用 subjects、connections、bindings。三个例子分别演示 create 分组、update 分组和同名 create。正文继续描述歧义待定、只读绑定、动物和组织适用范围。

沿用实际主体解码／物化测试和 20 组固定候选真实测试。真实测试只调用第二阶段，期望分组留在本机；没有创建数据库或调用 embedding。旧 10 组结果保持为 V23 历史记录。新版指标见 [主体维护验证](../subject-memory-validation.md)。
