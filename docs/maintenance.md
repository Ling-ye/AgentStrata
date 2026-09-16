# 文档与 SDD 维护

维护现行事实和可复用的方法。完整规则以 [文档治理基线](../specs/documentation-governance/spec.md) 为准；需要修改代码时先选 [检查范围](guides/development.md#选择检查范围)。

## 放在哪里

| 内容 | 归属 |
| --- | --- |
| 从任务进入下一页 | docs/README |
| 前提、步骤、成功判断与故障处理 | guides |
| 当前职责、机制、公开入口与修改边界 | reference |
| 长期架构规则或尚未完成的设计 | specs |
| 单次运行结果、临时失败、机器状态和日志 | 交付说明或 CI，不进入维护文档 |

新增正文先确认已有主题能否承载；一个事实只维护一处。篇幅和重复提示供审查，不为凑字数删约束。字段、命令和入口变化时核对关联页，不通过更新时间戳表示“已维护”。

## SDD 生命周期

架构、公共契约、部署和数据迁移先创建或引用规格；普通修复直接实现并测试。使用 [模板](../specs/_template/spec.md)，frontmatter 只包含 id/type/status/created，正文只需 Summary、Design、Acceptance、Verification。

探索为 draft，设计可实施为 accepted，代码、测试和文档完成后为 implemented。完成的普通设计将有效规则与必要理由迁入领域正文后删除；长期基线保留。无法确认的未完成条件列在 [规格索引](../specs/README.md)，不能因状态旧就删除。

## 检查与治理

```bash
.venv/bin/python scripts/check_docs.py --json
.venv/bin/python scripts/check_repo.py docs
```

链接、锚点、源码入口、孤立页面和明确运行流水是确定性错误。源码变化、篇幅、密集段落和重复只是审查提示；语义失真必须指出文档位置、源码依据和具体冲突。

仓库检查入口会自动关联本地变更，跨提交比较使用 `--docs-base`。逻辑段落或列表项超过
500 个可见字符时提示整理；手动折行不会改变计算，代码和表格等材料除外，不据此强制删字。

Harness 先看索引和本领域，再按源码链接读取必要材料。普通指南与说明入口可自动修复；reference、维护规范、SDD、AGENTS、Cursor 和检查规则只报告 needs_decision。新建或删除文档仍须通过全局导航和保护检查。规范修改由明确授权的维护任务完成。

检查器不执行代码块、不访问网络、不启动模型。固定夹具、配置阈值、合法版本和创建日期继续保留。

## 源码入口

- [文档检查与关联](../scripts/check_docs.py)
- [仓库检查和变更收集](../scripts/check_repo.py)
- [CI 基准传递](../.github/workflows/ci.yml)
