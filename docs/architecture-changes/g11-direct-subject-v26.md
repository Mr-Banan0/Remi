# V26：原文直接维护主体与关系

当前链路：原文与上下文 → 程序召回 → 一次主体模型 → 验证并固定身份 → 普通记忆提候选 → 召回旧记忆 → 普通记忆维护 → 原子提交。

## 保留、修改与退出

| 部分 | 本轮处理 |
| --- | --- |
| 主体范围与存储 | 保留 person／animal／organization、稳定 user、来源与修订、六表与 JSON schema_version=12 |
| 主体输入 | 修改为原文、上下文与旧目录；程序从原文匹配旧名称／别名，优先提供命中证据 |
| 主体输出 | 模型分配本轮身份 ref，直接给出名称、别名和局部证据；create／update／pending 与只读 bindings |
| 联系输出 | 直接给出主体 refs、原文、模式和 create／update／keep／pending；旧命题锚点与参与方向分别校验 |
| 普通记忆 | 保留两步处理；提候选移动到身份固定后，subject_refs 直接引用固定视图 |
| 退出 | 原主体 scopes／statements 提取、按名称字符串注册及归并、subject_choices、lookup_names、仅选择预制关系 candidate_id 的主体合同 |
| 程序保护 | 保留所有权、来源、图一致性、单次共享修正、幂等与原子提交；补齐错误句柄隔离、局部关系待定与依赖候选重算 |
| Pipeline | G11-direct-subject-v26，旧未完成检查点按原文重放 |

主体路径每处理单元一次正常模型调用。程序仍对每个用户文本单元运行普通候选提取，用于判断其余状态／事件；普通候选为空时共两次，有可维护普通记忆时共三次。输出修正、供应商重试和并发重算单独统计。

## 结构化调用示例

以下 Input 与 Assistant response 直接取自当前十组英文 SUBJECT_EXAMPLES。每例独立；临时 ref 连续分配，已有 ID 在目录中定义。示例覆盖首次创建 user、同名分离、别名归并、关系变化与引用说话者。

### 1. New subject with aliases

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Mira is also called M."
  },
  "context_sources": [],
  "subject_catalog": [],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "name": "Mira",
      "kind": "person",
      "aliases": [
        {
          "name": "Mira",
          "source_id": "S1",
          "quote": "Mira is also called M."
        },
        {
          "name": "M",
          "source_id": "S1",
          "quote": "Mira is also called M."
        }
      ],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "Mira is also called M."
        }
      ]
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 2. Existing subject gains an alias

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "My neighbor Mira is also called M."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "neighbor_mira",
      "name": "Mira",
      "kind": "person",
      "aliases": [
        {
          "name": "Mira",
          "quote": "Mira is my neighbor."
        }
      ]
    }
  ],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "update",
      "target_id": "neighbor_mira",
      "name": "Mira",
      "kind": "person",
      "aliases": [
        {
          "name": "Mira",
          "source_id": "S1",
          "quote": "My neighbor Mira is also called M."
        },
        {
          "name": "M",
          "source_id": "S1",
          "quote": "My neighbor Mira is also called M."
        }
      ],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "My neighbor Mira is also called M."
        }
      ]
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 3. Different subject with the same name

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I met a different Mira today."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "neighbor_mira",
      "name": "Mira",
      "kind": "person",
      "aliases": [
        {
          "name": "Mira",
          "quote": "Mira is my neighbor."
        }
      ]
    }
  ],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "name": "Mira",
      "kind": "person",
      "aliases": [
        {
          "name": "Mira",
          "source_id": "S1",
          "quote": "I met a different Mira today."
        }
      ],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I met a different Mira today."
        }
      ]
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 4. Create the user and an animal relationship

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I own the cat Juniper."
  },
  "context_sources": [],
  "subject_catalog": [],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "name": "@user",
      "kind": "person",
      "aliases": [],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I own the cat Juniper."
        }
      ]
    },
    {
      "ref": 1,
      "operation": "create",
      "target_id": null,
      "name": "Juniper",
      "kind": "animal",
      "aliases": [
        {
          "name": "Juniper",
          "source_id": "S1",
          "quote": "I own the cat Juniper."
        }
      ],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I own the cat Juniper."
        }
      ]
    }
  ],
  "connections": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I own the cat Juniper.",
      "mode": "asserted",
      "previous_quote": null
    }
  ],
  "bindings": []
}
```

### 5. End an existing relationship

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I no longer work at Harbor Club."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "speaker",
      "name": "user",
      "kind": "person",
      "is_user": true,
      "aliases": []
    },
    {
      "id": "harbor_club",
      "name": "Harbor Club",
      "kind": "organization",
      "is_user": false,
      "aliases": [
        {
          "name": "Harbor Club",
          "quote": "I work at Harbor Club."
        }
      ]
    }
  ],
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I work at Harbor Club.",
      "mode": "asserted",
      "ordered_subject_ids": [
        "speaker",
        "harbor_club"
      ]
    }
  ]
}
```

Assistant response

```json
{
  "subjects": [],
  "connections": [
    {
      "ref": 0,
      "operation": "update",
      "target_id": "employment",
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I no longer work at Harbor Club.",
      "mode": "asserted",
      "previous_quote": "I work at Harbor Club."
    }
  ],
  "bindings": [
    {
      "ref": 0,
      "target_id": "speaker",
      "mention": "@user",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I no longer work at Harbor Club."
        }
      ]
    },
    {
      "ref": 1,
      "target_id": "harbor_club",
      "mention": "Harbor Club",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I no longer work at Harbor Club."
        }
      ]
    }
  ]
}
```

### 6. Preserve separate relationship aspects

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I still work at Harbor Club. I now volunteer there too."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "speaker",
      "name": "user",
      "kind": "person",
      "is_user": true,
      "aliases": []
    },
    {
      "id": "harbor_club",
      "name": "Harbor Club",
      "kind": "organization",
      "is_user": false,
      "aliases": [
        {
          "name": "Harbor Club",
          "quote": "I work at Harbor Club."
        }
      ]
    }
  ],
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I work at Harbor Club.",
      "mode": "asserted",
      "ordered_subject_ids": [
        "speaker",
        "harbor_club"
      ]
    }
  ]
}
```

Assistant response

```json
{
  "subjects": [],
  "connections": [
    {
      "ref": 0,
      "operation": "keep",
      "target_id": "employment",
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I still work at Harbor Club.",
      "mode": "asserted",
      "previous_quote": "I work at Harbor Club."
    },
    {
      "ref": 1,
      "operation": "create",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I now volunteer there too.",
      "mode": "asserted",
      "previous_quote": null
    }
  ],
  "bindings": [
    {
      "ref": 0,
      "target_id": "speaker",
      "mention": "@user",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I still work at Harbor Club. I now volunteer there too."
        }
      ]
    },
    {
      "ref": 1,
      "target_id": "harbor_club",
      "mention": "Harbor Club",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I still work at Harbor Club. I now volunteer there too."
        }
      ]
    }
  ]
}
```

### 7. Resolve aliases before relationship endpoints

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "My dog Juniper is also called June. June lives with my sister Nora."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "dog_juniper",
      "name": "Juniper",
      "kind": "animal",
      "is_user": false,
      "aliases": [
        {
          "name": "Juniper",
          "quote": "My dog is Juniper."
        }
      ]
    },
    {
      "id": "sister_nora",
      "name": "Nora",
      "kind": "person",
      "is_user": false,
      "aliases": [
        {
          "name": "Nora",
          "quote": "Nora is my sister."
        }
      ]
    }
  ],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "update",
      "target_id": "dog_juniper",
      "name": "Juniper",
      "kind": "animal",
      "aliases": [
        {
          "name": "Juniper",
          "source_id": "S1",
          "quote": "My dog Juniper is also called June. June lives with my sister Nora."
        },
        {
          "name": "June",
          "source_id": "S1",
          "quote": "My dog Juniper is also called June. June lives with my sister Nora."
        }
      ],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "My dog Juniper is also called June. June lives with my sister Nora."
        }
      ]
    }
  ],
  "connections": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "June lives with my sister Nora.",
      "mode": "asserted",
      "previous_quote": null
    }
  ],
  "bindings": [
    {
      "ref": 1,
      "target_id": "sister_nora",
      "mention": "Nora",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "My dog Juniper is also called June. June lives with my sister Nora."
        }
      ]
    }
  ]
}
```

### 8. Keep desire separate from fact

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I hope to work at Harbor Club again."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "speaker",
      "name": "user",
      "kind": "person",
      "is_user": true,
      "aliases": []
    },
    {
      "id": "harbor_club",
      "name": "Harbor Club",
      "kind": "organization",
      "is_user": false,
      "aliases": [
        {
          "name": "Harbor Club",
          "quote": "I work at Harbor Club."
        }
      ]
    }
  ],
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I no longer work at Harbor Club.",
      "mode": "asserted",
      "ordered_subject_ids": [
        "speaker",
        "harbor_club"
      ]
    }
  ]
}
```

Assistant response

```json
{
  "subjects": [],
  "connections": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I hope to work at Harbor Club again.",
      "mode": "desired",
      "previous_quote": null
    }
  ],
  "bindings": [
    {
      "ref": 0,
      "target_id": "speaker",
      "mention": "@user",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I hope to work at Harbor Club again."
        }
      ]
    },
    {
      "ref": 1,
      "target_id": "harbor_club",
      "mention": "Harbor Club",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "I hope to work at Harbor Club again."
        }
      ]
    }
  ]
}
```

### 9. Keep ambiguous identity pending

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Alex works at our Harbor Club."
  },
  "context_sources": [],
  "subject_catalog": [
    {
      "id": "neighbor_alex",
      "name": "Alex",
      "kind": "person",
      "is_user": false,
      "aliases": [
        {
          "name": "Alex",
          "quote": "Alex is my neighbor."
        }
      ]
    },
    {
      "id": "cousin_alex",
      "name": "Alex",
      "kind": "person",
      "is_user": false,
      "aliases": [
        {
          "name": "Alex",
          "quote": "A different Alex is my cousin."
        }
      ]
    },
    {
      "id": "harbor_club",
      "name": "Harbor Club",
      "kind": "organization",
      "is_user": false,
      "aliases": [
        {
          "name": "Harbor Club",
          "quote": "Harbor Club is our club."
        }
      ]
    }
  ],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "pending",
      "target_id": null,
      "name": "Alex",
      "kind": "person",
      "aliases": [],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "Alex works at our Harbor Club."
        }
      ]
    }
  ],
  "connections": [
    {
      "ref": 0,
      "operation": "pending",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "Alex works at our Harbor Club.",
      "mode": "asserted",
      "previous_quote": null
    }
  ],
  "bindings": [
    {
      "ref": 1,
      "target_id": "harbor_club",
      "mention": "Harbor Club",
      "evidence": [
        {
          "source_id": "S1",
          "quote": "Alex works at our Harbor Club."
        }
      ]
    }
  ]
}
```

### 10. Preserve the quoted relationship speaker

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Yara wrote: 'I own Willow Studio.'"
  },
  "context_sources": [],
  "subject_catalog": [],
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "name": "Yara",
      "kind": "person",
      "aliases": [],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "Yara wrote: 'I own Willow Studio.'"
        }
      ]
    },
    {
      "ref": 1,
      "operation": "create",
      "target_id": null,
      "name": "Willow Studio",
      "kind": "organization",
      "aliases": [],
      "evidence": [
        {
          "source_id": "S1",
          "quote": "Yara wrote: 'I own Willow Studio.'"
        }
      ]
    }
  ],
  "connections": [
    {
      "ref": 0,
      "operation": "create",
      "target_id": null,
      "subject_refs": [
        0,
        1
      ],
      "source_id": "S1",
      "quote": "I own Willow Studio.",
      "mode": "document",
      "previous_quote": null
    }
  ],
  "bindings": []
}
```

验证结果见 [主体维护验证](../subject-memory-validation.md)。
