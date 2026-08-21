# SDD-lite

 AgentStrata 只对架构、公共契约、部署流程和数据迁移使用规格驱动开发。普通修复和局部功能直接实现并测试，避免为低风险改动维护重复清单。

## 目录与结构

 每个规格目录只包含 `specs/<id>/spec.md`，新规格从 `specs/_template/spec.md` 复制。

 `spec.md` 的 YAML frontmatter 只允许以下四个字段：

- `id`：必须与目录名一致。
- `type`：有效规格类型。
- `status`：`draft`、`accepted`、`implemented`、`superseded` 或 `rejected`。
- `created`：`YYYY-MM-DD`。

 正文固定包含且只包含一次以下一级章节，并保持顺序：

1. `Summary`
2. `Design`
3. `Acceptance`
4. `Verification`

 每个章节必须有非空内容；不再使用独立 acceptance/verification 文件、`allowed_paths`、implementation/documents 清单或强制 `PASS` 文本。

## 工作流

 设计阶段创建或更新规格，探索中使用 `draft`，设计可实施时改为 `accepted`，代码、测试和文档完成后改为 `implemented`。

 `Design` 记录必要的边界、取舍、兼容范围和回滚条件；`Acceptance` 写可观察结果；`Verification` 写实际或计划运行的命令与结果。历史规格迁移时允许把旧元数据和内容保留在对应正文中。

## 检查

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_sdd_specs.py -q --basetemp=/tmp/chatcopilot-pytest-sdd
```

 `scripts/check_sdd_specs.py` 只校验单文件 frontmatter、状态、章节结构和遗留规格文件；`tests/unit/test_sdd_specs.py` 覆盖同一规则。
