# Background Task Isolation & Completion Notification

Date: 2026-04-24

## Problem

ShellBgTool 的后台任务状态存储在模块级全局字典中，所有 channel/chat 共享。飞书群 A 启动的任务对群 B 完全可见（包括 list/output/kill）。此外，任务完成后没有通知机制，LLM 只能轮询。

## Design

### 1. Origin tracking — ContextVar + set_context

仿照 spawn tool 的模式：

- `BashTool` 增加三个 `ContextVar`：`_origin_channel`、`_origin_chat_id`、`_session_key`
- 增加实例属性 `_bus`（`MessageBus | None`），由 `set_context(bus, channel, chat_id, session_key)` 一次性设置
- `loop.py` 的 `_set_tool_context()` 方法已有 `name == "spawn"` 分支，增加 `name == "bash"` 分支调用 `set_context()`
- `_run_background()` 将 `channel`/`chat_id`/`session_key` 写入 `_bg_meta`

### 2. Session isolation — ShellBgTool filtering

- `ShellBgTool` 同样持有 `session_key`（通过 set_context 或 ContextVar）
- `list()` 只返回当前 session_key 下的任务
- `output()` 和 `kill()` 校验目标 task 属于当前 session，不匹配则返回 "not found"
- 全局字典结构不变，只加过滤层

### 3. Completion notification — _monitor_process sends InboundMessage

`_monitor_process()` 改造：

```
process exits
  → update _bg_meta (status, exit_code, end_time)
  → extract bus/channel/chat_id/session_key from meta
  → render bg_task_announce.md template
  → construct InboundMessage:
      channel = "system"
      sender_id = "bg_task"
      chat_id = f"{origin_channel}:{origin_chat_id}"
      content = rendered template
      session_key_override = origin_session_key
      metadata = {"injected_event": "bg_task_result", "bg_id": bg_id}
  → bus.publish_inbound(msg)
```

loop.py 已有 `channel="system"` 的 InboundMessage 处理逻辑（spawn 走这条路径），无需修改消息处理。

### 4. Immediate return hint

`_run_background()` 的返回消息改为：

```
Background task started.
bash_bg_id: {bg_id}
Command: {command}

You will be notified when the task completes. No need to poll.
Use `shell_bg(action='output', bash_bg_id='{bg_id}')` to check output at any time.
Use `shell_bg(action='kill', bash_bg_id='{bg_id}')` to terminate.
```

去掉旧的 `Use shell_bg to check` 风格提示，明确告知 agent 会收到通知。

### 5. New template

`templates/agent/bg_task_announce.md`:

```markdown
Background task `{{bg_id}}` completed (exit code: {{exit_code}}).

Command: `{{command}}`

Use `shell_bg(action='output', bash_bg_id='{{bg_id}}')` to view the output.
```

## Files changed

| File | Change |
|------|--------|
| `nanobot/agent/tools/shell.py` | BashTool: ContextVar + set_context + bus; _run_background stores origin; _monitor_process sends notification; ShellBgTool session filtering; updated return message |
| `nanobot/agent/loop.py` | `_set_tool_context()` adds bash case |
| `templates/agent/bg_task_announce.md` | New notification template |
| `tests/test_shell_tool.py` | Isolation and notification tests |

## Not changed

runner.py, subagent.py, bus/, config/schema.py
