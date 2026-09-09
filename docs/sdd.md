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

## 运行时架构基线

[机器人运行时四层架构基线](../specs/runtime-four-layer-definition/spec.md) 是后续设计和开发
必须遵循的长期标准：**渠道适配（Channel）→ Gateway → Application → Agent**。启动装配、
实例宿主、Console 和 Evaluation 位于四层之外；Contracts、Core、授权、模型访问、工具和存储
是支撑模块。基线同时规定结构化交接、依赖方向及合法的直接入口和提前终止，不要求每次操作走完四层。

- 涉及运行时架构、跨层契约、运行部署或相关数据迁移的规格，必须引用该基线，并在现有
  `Design` 中说明受影响职责、交接契约与依赖方向。
- 启动装配、Console、Evaluation 的设计按需说明与运行时的边界，不将自身定义为消息必经层。
- 普通修复仍按本规范原有范围直接实现并测试，不增加统一填表、“不适用”声明或额外审批步骤。
- 与基线冲突的设计必须明确提出基线变更并单独审议，同时更新规则与验证；不能在局部规格中
  悄悄改层级。Legacy 是待退出存量，不构成另一套标准，新设计不得扩展旧运行链。
- 历史规格保留当次实施背景和验证结论，引用现行基线时说明后续关系，不把旧分层描述用作当前规则。

不增加 frontmatter 字段或顶层章节，也不为本规范建立第二份架构定义。

## 检查

```bash
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
.venv/bin/python -m pytest tests/unit/test_sdd_specs.py -q --basetemp=/tmp/chatcopilot-pytest-sdd
```

`scripts/check_sdd_specs.py` 只校验单文件 frontmatter、状态、章节结构和遗留规格文件；
`tests/unit/test_sdd_specs.py` 覆盖同一规则。`scripts/check_architecture.py` 继续使用现有规则检查
静态依赖，两个检查都已接入仓库 `fast/full`。涉及运行时的改动需执行相关检查和行为测试。

职责归属由规范与评审约束，跨层行为由针对性测试验证；格式检查和 import 检查不能证明全部运行
语义，也不作为真实模型或 QQ 端到端证据。不用关键词扫描替代架构语义验证。
