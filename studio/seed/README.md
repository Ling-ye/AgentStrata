# 桌面应用开发接口

`index.html` 是独立桌面窗口的入口。页面可以自定义界面。脚本和样式使用随应用分发的本地文件，事件在脚本中绑定。窗口只开放本应用的接口：

- `desktop.chat(text)` 通过应用自己的 Gateway 请求 Agent，返回本次回复。
- `desktop.action(name, args)` 调用本应用的一个业务操作。页面和 Agent 使用 `actions.py` 中同一份 `actions(data)` 注册表。函数使用类型注解和文档字符串供 MCP 发现，持久化业务数据保存在传入的本应用目录。
- `desktop.settings()` 打开模型设置，`desktop.source()` 打开随安装包交付的对应源码。

例如，勾选一条记录和对 Agent 说“标为完成”应调用同一个业务操作，完成后从同一数据源刷新页面。
