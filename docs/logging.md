# 日志契约

## 文件与轮转

- 目录：`%APPDATA%\mini-ide\logs\`
- 文件：`mini-ide-YYYYMMDD.log`
- 基础 logger：`mini-ide`
- 行为 logger：`mini-ide.action`，通过父 logger 写入同一日志文件。
- 单文件最大 5 MB，保留 3 个轮转副本。
- 启动时清理超过 14 天的日志。
- GUI 菜单“帮助 → 打开 mini-ide 日志”和“打开日志目录”可直接访问。

## CLI 行为日志

CLI 请求统一记录：

~~~text
[CLI] <cmd> params=<参数字典>
[CLI] <cmd> ok
[CLI] <cmd> fail: <错误信息>
[CLI] <cmd> EXCEPTION
~~~

查询当天日志：

~~~powershell
$log = "$env:APPDATA\mini-ide\logs\mini-ide-$(Get-Date -f yyyyMMdd).log"
Select-String "\[CLI\]|\[GUI\]" $log
Select-String "(fail|FAIL|WARNING|ERROR).*\[CLI\]|\[GUI\].*失败" $log
~~~

## GUI 行为日志

当前使用 `[GUI]` 行为前缀记录的操作包括：

~~~text
启动服务、停止服务、重启服务、启动模块、停止模块
进入工作区、创建工作区、同步并推送工作区、合并并推送工作区、删除工作区
~~~

工作区操作会记录成功、失败和冲突回滚；同步并推送固定 fetch 远端，并记录是否保留冲突。CLI 单独执行同步或提交推送时继续记录对应命令。服务操作会记录项目、模块或 profile。

启动、打开项目、会话恢复、关闭窗口隐藏到托盘、异常、主线程卡死和托盘不可用等基础运行日志使用 `mini-ide.*` logger，未必带 `[GUI]` 前缀。排查这些问题时应直接查看当天完整日志，不能只依赖 `[GUI]` 筛选。

## 性能日志

性能专项使用 `mini-ide.performance` logger，格式为：

~~~text
perf op=<操作> duration_ms=<毫秒> files=<数量> matches=<数量> status=<状态>
~~~

当前覆盖会话恢复、文件索引、文件监控启动、文件名搜索、全局内容搜索、目录扫描与批量渲染、日志慢批次渲染、Git 状态/分支/脏检查/概览/diff worker、Git 结果渲染、状态轮询、资源守卫和 UI 事件循环延迟。文件监控日志使用 `file-watchdog-start`；目录日志使用 `directory-scan` 和 `directory-render`；日志单批渲染超过 100ms 时记录 `log-render`。这些日志只记录耗时与数量。搜索日志只记录 `query_len`，不记录搜索文本或文件内容。事件循环额外延迟达到 250ms 会记录 `ui-stall`；现有超过 3 秒的 freeze watchdog 线程栈仍保留。

应用真正退出时，若仍有搜索线程，`mini-ide.search_lifecycle` 会记录停止与等待结果；正常关闭窗口只隐藏到托盘，不触发该清理。

查询示例：

~~~powershell
$log = "$env:APPDATA\mini-ide\logs\mini-ide-$(Get-Date -f yyyyMMdd).log"
Select-String "mini-ide.performance.*perf op=" $log
Select-String "ui-stall|主线程疑似卡死" $log
~~~
