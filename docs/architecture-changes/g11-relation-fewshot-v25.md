# V25：主体与关系的英文 few-shot

主体维护沿用“确认身份与分组 → 主体增量／只读绑定 → 关系增量 → 程序校验”。下面九组 Input 与 Assistant response 直接取自当前 SUBJECT_EXAMPLES；供应商调用将它们分别序列化为 user 与 assistant 消息。

模型按 subjects、connections、bindings 合同返回 JSON 操作计划。ref 对应本例 proposal.subjects 中的候选；candidate_id 对应 proposal.connections 中的候选；旧 target_id 对应本例目录中的记录，新建使用 null。程序校验计划，复制候选证据并分配新 ID，然后执行写入。每例独立给出自己的完整输入。

## Executable few-shot examples

### 1. New subject, two names

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Mira is also called M."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Mira",
        "kind": "person",
        "aliases": [
          "Mira"
        ]
      },
      {
        "ref": 1,
        "name": "M",
        "kind": "person",
        "aliases": [
          "M"
        ]
      }
    ],
    "connections": []
  },
  "subject_catalog": [],
  "subject_choices": {
    "0": [],
    "1": []
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "operation": "create",
      "subject_refs": [
        0,
        1
      ],
      "target_id": null
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 2. Existing subject, new name

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "My neighbor Mira is also called M."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Mira",
        "kind": "person",
        "aliases": [
          "Mira"
        ]
      },
      {
        "ref": 1,
        "name": "M",
        "kind": "person",
        "aliases": [
          "M"
        ]
      }
    ],
    "connections": []
  },
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
  "subject_choices": {
    "0": [
      "neighbor_mira"
    ],
    "1": [
      "neighbor_mira"
    ]
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "operation": "update",
      "subject_refs": [
        0,
        1
      ],
      "target_id": "neighbor_mira"
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 3. Different subject, same name

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I met a different Mira today."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Mira",
        "kind": "person",
        "aliases": [
          "Mira"
        ]
      }
    ],
    "connections": []
  },
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
  "subject_choices": {
    "0": [
      "neighbor_mira"
    ]
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "operation": "create",
      "subject_refs": [
        0
      ],
      "target_id": null
    }
  ],
  "connections": [],
  "bindings": []
}
```

### 4. Create a relationship

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Nora owns the cat Juniper."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Nora",
        "kind": "person",
        "aliases": [
          "Nora"
        ]
      },
      {
        "ref": 1,
        "name": "Juniper",
        "kind": "animal",
        "aliases": [
          "Juniper"
        ]
      }
    ],
    "connections": [
      {
        "id": "owns_cat",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "Nora owns the cat Juniper.",
        "mode": "asserted"
      }
    ]
  },
  "subject_catalog": [],
  "subject_choices": {
    "0": [],
    "1": []
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "operation": "create",
      "subject_refs": [
        0
      ],
      "target_id": null
    },
    {
      "operation": "create",
      "subject_refs": [
        1
      ],
      "target_id": null
    }
  ],
  "connections": [
    {
      "candidate_id": "owns_cat",
      "operation": "create",
      "target_id": null
    }
  ],
  "bindings": []
}
```

### 5. Update an existing relationship

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I no longer work at Harbor Club."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "@user",
        "kind": "person",
        "aliases": []
      },
      {
        "ref": 1,
        "name": "Harbor Club",
        "kind": "organization",
        "aliases": [
          "Harbor Club"
        ]
      }
    ],
    "connections": [
      {
        "id": "ended_job",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "I no longer work at Harbor Club.",
        "mode": "asserted"
      }
    ]
  },
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
  "subject_choices": {
    "0": [
      "speaker"
    ],
    "1": [
      "harbor_club"
    ]
  },
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I work at Harbor Club.",
      "mode": "asserted"
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
      "candidate_id": "ended_job",
      "operation": "update",
      "target_id": "employment"
    }
  ],
  "bindings": [
    {
      "subject_refs": [
        0
      ],
      "target_id": "speaker"
    },
    {
      "subject_refs": [
        1
      ],
      "target_id": "harbor_club"
    }
  ]
}
```

### 6. Separate relationship aspects

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I still work at Harbor Club. I now volunteer there too."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "@user",
        "kind": "person",
        "aliases": []
      },
      {
        "ref": 1,
        "name": "Harbor Club",
        "kind": "organization",
        "aliases": [
          "Harbor Club"
        ]
      }
    ],
    "connections": [
      {
        "id": "same_job",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "I still work at Harbor Club.",
        "mode": "asserted"
      },
      {
        "id": "volunteering",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "I now volunteer there too.",
        "mode": "asserted"
      }
    ]
  },
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
  "subject_choices": {
    "0": [
      "speaker"
    ],
    "1": [
      "harbor_club"
    ]
  },
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I work at Harbor Club.",
      "mode": "asserted"
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
      "candidate_id": "volunteering",
      "operation": "create",
      "target_id": null
    }
  ],
  "bindings": [
    {
      "subject_refs": [
        0
      ],
      "target_id": "speaker"
    },
    {
      "subject_refs": [
        1
      ],
      "target_id": "harbor_club"
    }
  ]
}
```

### 7. Resolve aliases before choosing relationship endpoints

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "My dog Juniper is also called June. June lives with my sister Nora."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Juniper",
        "kind": "animal",
        "aliases": [
          "Juniper"
        ]
      },
      {
        "ref": 1,
        "name": "June",
        "kind": "animal",
        "aliases": [
          "June"
        ]
      },
      {
        "ref": 2,
        "name": "Nora",
        "kind": "person",
        "aliases": [
          "Nora"
        ]
      }
    ],
    "connections": [
      {
        "id": "lives_with",
        "subject_refs": [
          1,
          2
        ],
        "source_id": "S1",
        "quote": "June lives with my sister Nora.",
        "mode": "asserted"
      }
    ]
  },
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
  "subject_choices": {
    "0": [
      "dog_juniper"
    ],
    "1": [
      "dog_juniper"
    ],
    "2": [
      "sister_nora"
    ]
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [
    {
      "operation": "update",
      "subject_refs": [
        0,
        1
      ],
      "target_id": "dog_juniper"
    }
  ],
  "connections": [
    {
      "candidate_id": "lives_with",
      "operation": "create",
      "target_id": null
    }
  ],
  "bindings": [
    {
      "subject_refs": [
        2
      ],
      "target_id": "sister_nora"
    }
  ]
}
```

### 8. Preserve desire separately from fact

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "I hope to work at Harbor Club again."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "@user",
        "kind": "person",
        "aliases": []
      },
      {
        "ref": 1,
        "name": "Harbor Club",
        "kind": "organization",
        "aliases": [
          "Harbor Club"
        ]
      }
    ],
    "connections": [
      {
        "id": "wish",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "I hope to work at Harbor Club again.",
        "mode": "desired"
      }
    ]
  },
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
  "subject_choices": {
    "0": [
      "speaker"
    ],
    "1": [
      "harbor_club"
    ]
  },
  "relation_catalog": [
    {
      "id": "employment",
      "endpoint_ids": [
        "speaker",
        "harbor_club"
      ],
      "content": "I no longer work at Harbor Club.",
      "mode": "asserted"
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
      "candidate_id": "wish",
      "operation": "create",
      "target_id": null
    }
  ],
  "bindings": [
    {
      "subject_refs": [
        0
      ],
      "target_id": "speaker"
    },
    {
      "subject_refs": [
        1
      ],
      "target_id": "harbor_club"
    }
  ]
}
```

### 9. Leave an unresolved endpoint unassigned

Input

```json
{
  "new_user_source": {
    "id": "S1",
    "role": "user",
    "content": "Alex works at our Harbor Club."
  },
  "proposal": {
    "subjects": [
      {
        "ref": 0,
        "name": "Alex",
        "kind": "person",
        "aliases": [
          "Alex"
        ]
      },
      {
        "ref": 1,
        "name": "Harbor Club",
        "kind": "organization",
        "aliases": [
          "Harbor Club"
        ]
      }
    ],
    "connections": [
      {
        "id": "job",
        "subject_refs": [
          0,
          1
        ],
        "source_id": "S1",
        "quote": "Alex works at our Harbor Club.",
        "mode": "asserted"
      }
    ]
  },
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
  "subject_choices": {
    "0": [
      "neighbor_alex",
      "cousin_alex"
    ],
    "1": [
      "harbor_club"
    ]
  },
  "relation_catalog": []
}
```

Assistant response

```json
{
  "subjects": [],
  "connections": [],
  "bindings": [
    {
      "subject_refs": [
        1
      ],
      "target_id": "harbor_club"
    }
  ]
}
```

关系补丁继续只选择 candidate_id、operation、target_id；程序复制候选端点、准确原文、来源和表达模式。绑定供后续联系使用；无变化的关系省略补丁，身份未明确的关系保留待定。每个旧 ID 都来自对应目录，新主体和新关系的 ID 由程序分配。

## 局部改动与边界

| 部分 | 本轮处理 |
| --- | --- |
| SUBJECT_EXAMPLES | 3 → 9；主体示例的旧 ID 改为带含义的 neighbor_mira，补充六个关系示例 |
| Prompt 正文、引用协议与另外两阶段示例 | 保持 |
| 主体／关系生产解码、存储、召回与图投影 | 保持 |
| Pipeline 版本 | 更新为 G11-relation-fewshot-v25，阶段检查点绑定本轮示例 |
| 测试驱动 | 校验真实主体／关系解码，增加八个不同虚构称呼的关系场景及错误操作检测 |
| 数据库与调用预算 | 继续使用既有六表；生产普通单元三次逻辑调用，本轮模型诊断只调用主体阶段并使用内存记录 |

原十例与三例实验继续保留各自冻结运行时。新示例直接进入当前主体维护调用；本轮没有增加生产分支、关系枚举或数据集专用规则。验证结果与适用范围见 [主体维护验证](../subject-memory-validation.md)。
