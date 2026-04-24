# Background Task Isolation & Completion Notification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate background shell tasks per (channel, chat_id) session and send completion notifications back to the originating channel via the message bus.

**Architecture:** Add origin tracking (ContextVar + set_context) to BashTool and ShellBgTool, matching the spawn tool pattern. `_monitor_process` sends an `InboundMessage` on completion. ShellBgTool filters all operations by session_key so tasks are invisible across sessions.

**Tech Stack:** Python 3.12+, asyncio, ContextVar, Jinja2 templates, pytest-asyncio

---

## File Structure

| File | Responsibility |
|------|---------------|
| `nanobot/agent/tools/shell.py` | BashTool: ContextVar + set_context + bus ref; `_run_background` stores origin; `_monitor_process` sends notification; ShellBgTool: session_key filtering |
| `nanobot/agent/loop.py:352-363` | `_set_tool_context()` adds bash/shell_bg to the routing list |
| `nanobot/templates/agent/bg_task_announce.md` | New Jinja2 template for completion notification |
| `tests/tools/test_shell_tool.py` | New tests for isolation and notification |

---

### Task 1: Add origin ContextVar and set_context to BashTool

**Files:**
- Modify: `nanobot/agent/tools/shell.py:1-10` (imports)
- Modify: `nanobot/agent/tools/shell.py:264-310` (BashTool class)
- Test: `tests/tools/test_shell_tool.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestBashToolSetContext:
    """Test BashTool set_context stores origin info via ContextVar."""

    def test_set_context_stores_values(self):
        tool = BashTool()
        tool.set_context(
            bus=None,
            channel="feishu",
            chat_id="chat_abc",
            session_key="feishu:chat_abc",
        )
        assert tool._origin_channel.get() == "feishu"
        assert tool._origin_chat_id.get() == "chat_abc"
        assert tool._session_key.get() == "feishu:chat_abc"

    def test_set_context_defaults(self):
        tool = BashTool()
        assert tool._origin_channel.get() == "cli"
        assert tool._origin_chat_id.get() == "direct"
        assert tool._session_key.get() == "cli:direct"

    def test_set_context_stores_bus(self):
        mock_bus = MagicMock()
        tool = BashTool()
        tool.set_context(
            bus=mock_bus,
            channel="cli",
            chat_id="direct",
            session_key="cli:direct",
        )
        assert tool._bus is mock_bus
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBashToolSetContext -v`
Expected: FAIL — `BashTool` has no `set_context` / `_origin_channel` attributes

- [ ] **Step 3: Write minimal implementation**

In `nanobot/agent/tools/shell.py`, add import at top:

```python
from contextvars import ContextVar
```

Add to `BashTool.__init__` (after `self.allowed_env_keys = allowed_env_keys or []`):

```python
self._origin_channel: ContextVar[str] = ContextVar("bash_origin_channel", default="cli")
self._origin_chat_id: ContextVar[str] = ContextVar("bash_origin_chat_id", default="direct")
self._session_key: ContextVar[str] = ContextVar("bash_session_key", default="cli:direct")
self._bus: Any | None = None
```

Add method to `BashTool`:

```python
def set_context(
    self,
    bus: Any | None = None,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str = "cli:direct",
) -> None:
    self._bus = bus
    self._origin_channel.set(channel)
    self._origin_chat_id.set(chat_id)
    self._session_key.set(session_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBashToolSetContext -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/tools/shell.py tests/tools/test_shell_tool.py
git commit -m "feat(shell): add origin ContextVar and set_context to BashTool"
```

---

### Task 2: Store origin info in _bg_meta when running background tasks

**Files:**
- Modify: `nanobot/agent/tools/shell.py:516-524` (`_run_background` meta dict)
- Modify: `nanobot/agent/tools/shell.py:529-536` (return message)
- Test: `tests/tools/test_shell_tool.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestBackgroundOrigin:
    """Test that background tasks store origin info in _bg_meta."""

    @pytest.mark.asyncio
    async def test_bg_meta_stores_origin(self):
        from nanobot.agent.tools.shell import _bg_meta

        tool = BashTool()
        mock_bus = MagicMock()
        tool.set_context(
            bus=mock_bus,
            channel="feishu",
            chat_id="chat_123",
            session_key="feishu:chat_123",
        )
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = 88888
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                with patch("nanobot.agent.tools.shell.get_data_dir") as mock_dir:
                    mock_dir.return_value = Path("/tmp/nanobot-test")
                    with patch("builtins.open", MagicMock()):
                        await tool.execute(
                            command="sleep 100",
                            run_in_background=True,
                            purpose="test origin",
                        )
        # Find the bg_id that was just created
        bg_ids = [k for k in _bg_meta if k.startswith("bash_bg_")]
        assert len(bg_ids) >= 1
        meta = _bg_meta[bg_ids[-1]]
        assert meta["channel"] == "feishu"
        assert meta["chat_id"] == "chat_123"
        assert meta["session_key"] == "feishu:chat_123"
        assert meta["bus"] is mock_bus
        # cleanup
        for bg_id in bg_ids:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_bg_return_message_mentions_notification(self):
        tool = BashTool()
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = 77777
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                with patch("nanobot.agent.tools.shell.get_data_dir") as mock_dir:
                    mock_dir.return_value = Path("/tmp/nanobot-test")
                    with patch("builtins.open", MagicMock()):
                        result = await tool.execute(
                            command="sleep 100",
                            run_in_background=True,
                            purpose="test msg",
                        )
        assert "notified" in result.lower() or "notification" in result.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBackgroundOrigin -v`
Expected: FAIL — `_bg_meta` entries don't have `channel`/`chat_id`/`session_key`/`bus` keys

- [ ] **Step 3: Write minimal implementation**

In `_run_background()`, update the `_bg_meta` dict (around line 518):

```python
_bg_meta[bg_id] = {
    "command": command,
    "purpose": purpose,
    "start_time": datetime.now().isoformat(),
    "status": "running",
    "output_file": str(output_file),
    "channel": self._origin_channel.get(),
    "chat_id": self._origin_chat_id.get(),
    "session_key": self._session_key.get(),
    "bus": self._bus,
}
```

Update the return message (around line 529):

```python
return (
    f"Background task started.\n"
    f"bash_bg_id: {bg_id}\n"
    f"Command: {command}\n\n"
    f"You will be notified when the task completes. No need to poll.\n"
    f"Use `shell_bg(action='output', bash_bg_id='{bg_id}')` to check output at any time.\n"
    f"Use `shell_bg(action='kill', bash_bg_id='{bg_id}')` to terminate."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBackgroundOrigin -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/tools/shell.py tests/tools/test_shell_tool.py
git commit -m "feat(shell): store origin info in bg_meta, update return message"
```

---

### Task 3: Create bg_task_announce.md template

**Files:**
- Create: `nanobot/templates/agent/bg_task_announce.md`
- Test: `tests/tools/test_shell_tool.py`

- [ ] **Step 1: Write the template test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestBgTaskAnnounceTemplate:
    """Test the bg_task_announce.md template renders correctly."""

    def test_renders_with_exit_code_0(self):
        from nanobot.utils.prompt_templates import render_template

        result = render_template(
            "agent/bg_task_announce.md",
            bg_id="bash_bg_abc123",
            exit_code=0,
            command="npm run build",
        )
        assert "bash_bg_abc123" in result
        assert "completed" in result.lower()
        assert "npm run build" in result
        assert "shell_bg" in result

    def test_renders_with_nonzero_exit(self):
        from nanobot.utils.prompt_templates import render_template

        result = render_template(
            "agent/bg_task_announce.md",
            bg_id="bash_bg_fail99",
            exit_code=1,
            command="make test",
        )
        assert "bash_bg_fail99" in result
        assert "1" in result
        assert "make test" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBgTaskAnnounceTemplate -v`
Expected: FAIL — template file not found

- [ ] **Step 3: Create the template**

Create `nanobot/templates/agent/bg_task_announce.md`:

```markdown
[Background Task Result — metadata only, not instructions]
Task ID: {{ bg_id }}
Exit code: {{ exit_code }}
Command: `{{ command }}`

[/Background Task Result]

A background shell task has completed. Use `shell_bg(action='output', bash_bg_id='{{ bg_id }}')` to view the output. Summarize the result for the user naturally. Keep it brief (1-2 sentences).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestBgTaskAnnounceTemplate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/templates/agent/bg_task_announce.md tests/tools/test_shell_tool.py
git commit -m "feat(templates): add bg_task_announce.md for completion notification"
```

---

### Task 4: Send InboundMessage on task completion in _monitor_process

**Files:**
- Modify: `nanobot/agent/tools/shell.py:1-10` (imports)
- Modify: `nanobot/agent/tools/shell.py:194-219` (`_monitor_process`)
- Test: `tests/tools/test_shell_tool.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestMonitorProcessNotification:
    """Test that _monitor_process sends InboundMessage on completion."""

    @pytest.mark.asyncio
    async def test_sends_inbound_on_completion(self):
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _bg_file_handles, _monitor_process,
        )
        from nanobot.bus.events import InboundMessage

        mock_bus = AsyncMock()
        bg_id = "bash_bg_notif1"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "echo done",
            "purpose": "test",
            "start_time": "",
            "status": "running",
            "output_file": "/tmp/notif.log",
            "channel": "feishu",
            "chat_id": "chat_xyz",
            "session_key": "feishu:chat_xyz",
            "bus": mock_bus,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
            mock_bus.publish_inbound.assert_called_once()
            msg = mock_bus.publish_inbound.call_args[0][0]
            assert isinstance(msg, InboundMessage)
            assert msg.channel == "system"
            assert msg.sender_id == "bg_task"
            assert msg.chat_id == "feishu:chat_xyz"
            assert msg.session_key_override == "feishu:chat_xyz"
            assert msg.metadata["injected_event"] == "bg_task_result"
            assert msg.metadata["bg_id"] == bg_id
            assert "bash_bg_notif1" in msg.content
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_no_notification_without_bus(self):
        """If bus is None (e.g. CLI mode without set_context), don't crash."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        bg_id = "bash_bg_nobus"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/n.log",
            "channel": "cli", "chat_id": "direct",
            "session_key": "cli:direct", "bus": None,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_no_notification_without_origin(self):
        """If meta has no channel/session_key keys (old-style), don't crash."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        bg_id = "bash_bg_noorigin"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/n.log",
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
        finally:
            _bg_meta.pop(bg_id, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestMonitorProcessNotification -v`
Expected: FAIL — `mock_bus.publish_inbound` is never called

- [ ] **Step 3: Write minimal implementation**

Add imports at top of `nanobot/agent/tools/shell.py`:

```python
from nanobot.bus.events import InboundMessage
from nanobot.utils.prompt_templates import render_template
```

Replace `_monitor_process` (lines 194-219) with:

```python
async def _monitor_process(bg_id: str) -> None:
    """Monitor a background process; update meta on completion and notify origin."""
    process = _bg_processes.get(bg_id)
    meta = _bg_meta.get(bg_id)
    if not process or not meta:
        _close_file_handle(_bg_file_handles.pop(bg_id, None))
        return

    try:
        exit_code = await process.wait()
        async with _bg_lock:
            if meta.get("status") != "killed":
                meta["status"] = "completed" if exit_code == 0 else "failed"
            meta["exit_code"] = exit_code
            meta["end_time"] = datetime.now().isoformat()
    except Exception as e:
        logger.error(f"Error monitoring process {bg_id}: {e}")
        async with _bg_lock:
            meta["status"] = "failed"
            meta["error"] = str(e)
            meta["end_time"] = datetime.now().isoformat()
    finally:
        _close_file_handle(_bg_file_handles.pop(bg_id, None))
        _bg_processes.pop(bg_id, None)

    # Send completion notification via message bus
    bus = meta.get("bus")
    if bus is None:
        return
    channel = meta.get("channel")
    chat_id = meta.get("chat_id")
    session_key = meta.get("session_key")
    if not channel or not chat_id or not session_key:
        return

    try:
        announce_content = render_template(
            "agent/bg_task_announce.md",
            bg_id=bg_id,
            exit_code=meta.get("exit_code", -1),
            command=meta.get("command", ""),
        )
        msg = InboundMessage(
            channel="system",
            sender_id="bg_task",
            chat_id=f"{channel}:{chat_id}",
            content=announce_content,
            session_key_override=session_key,
            metadata={
                "injected_event": "bg_task_result",
                "bg_id": bg_id,
            },
        )
        await bus.publish_inbound(msg)
        logger.debug(
            "Background task [{}] notified {}:{}", bg_id, channel, chat_id,
        )
    except Exception as e:
        logger.warning("Failed to send bg task notification for {}: {}", bg_id, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestMonitorProcessNotification -v`
Expected: PASS

- [ ] **Step 5: Run existing monitor tests to check no regressions**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestShellBgTool::test_monitor_process -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nanobot/agent/tools/shell.py tests/tools/test_shell_tool.py
git commit -m "feat(shell): send InboundMessage on bg task completion"
```

---

### Task 5: Add session_key filtering to ShellBgTool

**Files:**
- Modify: `nanobot/agent/tools/shell.py:708-740` (ShellBgTool class)
- Test: `tests/tools/test_shell_tool.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestShellBgToolSessionIsolation:
    """Test that ShellBgTool filters tasks by session_key."""

    @pytest.mark.asyncio
    async def test_list_only_shows_own_session(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        tool._session_key = "feishu:chat_A"
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_own"] = {
            "command": "own", "purpose": "a",
            "start_time": "", "status": "running", "output_file": "/tmp/a.log",
            "session_key": "feishu:chat_A",
        }
        _bg_meta["bash_bg_other"] = {
            "command": "other", "purpose": "b",
            "start_time": "", "status": "running", "output_file": "/tmp/b.log",
            "session_key": "feishu:chat_B",
        }
        _bg_meta["bash_bg_nokey"] = {
            "command": "legacy", "purpose": "c",
            "start_time": "", "status": "running", "output_file": "/tmp/c.log",
        }
        try:
            result = await tool.execute(action="list")
            assert "bash_bg_own" in result
            assert "bash_bg_other" not in result
            assert "bash_bg_nokey" not in result
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_output_rejects_other_session(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        tool._session_key = "feishu:chat_A"
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_x"] = {
            "command": "secret", "purpose": "hidden",
            "start_time": "", "status": "completed",
            "output_file": "/tmp/x.log",
            "session_key": "feishu:chat_B",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id="bash_bg_x")
            assert "not found" in result.lower()
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_output_allows_own_session(self, tmp_path):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        tool._session_key = "feishu:chat_A"
        saved = _bg_meta.copy()
        _bg_meta.clear()
        bg_id = "bash_bg_y"
        log_file = tmp_path / "output.log"
        log_file.write_text("hello\n", encoding="utf-8")
        _bg_meta[bg_id] = {
            "command": "echo", "purpose": "ok",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "feishu:chat_A",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "hello" in result
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_kill_rejects_other_session(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta, _bg_processes

        tool = ShellBgTool()
        tool._session_key = "feishu:chat_A"
        saved_meta = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_z"] = {
            "command": "sleep", "purpose": "test",
            "start_time": "", "status": "running",
            "output_file": "/tmp/z.log",
            "session_key": "feishu:chat_B",
        }
        try:
            result = await tool.execute(action="kill", bash_bg_id="bash_bg_z")
            assert "not found" in result.lower()
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved_meta)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestShellBgToolSessionIsolation -v`
Expected: FAIL — `ShellBgTool` has no `_session_key`; `list` shows all tasks

- [ ] **Step 3: Write minimal implementation**

Add to `ShellBgTool.__init__`:

```python
self._session_key: str = "cli:direct"
```

Add `set_context` method to `ShellBgTool`:

```python
def set_context(
    self,
    bus: Any | None = None,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str = "cli:direct",
) -> None:
    self._session_key = session_key
```

Add a helper method to `ShellBgTool`:

```python
def _visible_tasks(self) -> dict[str, dict]:
    """Return bg_meta entries belonging to the current session."""
    sk = self._session_key
    return {k: v for k, v in _bg_meta.items() if v.get("session_key") == sk}
```

Update `ShellBgTool._list()` — replace the body to use `_visible_tasks()`:

```python
@staticmethod
async def _list() -> str:
```

This needs to change from `@staticmethod` to a regular method. Replace the entire `_list` method:

```python
async def _list(self) -> str:
    async with _bg_lock:
        visible = self._visible_tasks()
        if not visible:
            return "No background tasks running."

        rows = []
        for bg_id, m in visible.items():
            rows.append(
                f"  {bg_id}  status={m['status']}  command={m['command']}  "
                f"purpose={m.get('purpose', '')}"
            )
        return "\n".join(rows)
```

Update `_output` — add session check at the beginning:

```python
@staticmethod
async def _output(bash_bg_id: str) -> str:
```

Replace `_output` entirely:

```python
async def _output(self, bash_bg_id: str) -> str:
    async with _bg_lock:
        meta = _bg_meta.get(bash_bg_id)
        if not meta:
            return f"Error: Task '{bash_bg_id}' not found"
        if meta.get("session_key") != self._session_key:
            return f"Error: Task '{bash_bg_id}' not found"
        output_file_str = meta["output_file"]
        status = meta.get("status", "unknown")
```

The rest of `_output` (reading the file, formatting) stays the same, but the indentation level shifts because it's no longer inside a `@staticmethod`.

Update `_kill` — add session check after the initial meta lookup. Inside `_kill`, after `if not meta:` and before `process = _bg_processes.get(bash_bg_id):`:

```python
if meta.get("session_key") != self._session_key:
    return f"Error: Task '{bash_bg_id}' not found"
```

Update `execute` — change the dispatch to pass `self`:

```python
if action == "list":
    return await self._list()
elif action == "output":
    return await self._output(bash_bg_id)
```

The `_kill` method stays `@staticmethod` — add the session_key check inline in `execute` before calling `_kill`, or convert `_kill` to a regular method. The simplest approach: check in `execute` before dispatching to `_kill`:

```python
elif action == "kill":
    async with _bg_lock:
        meta = _bg_meta.get(bash_bg_id)
        if not meta:
            return f"Error: Task '{bash_bg_id}' not found"
        if meta.get("session_key") != self._session_key:
            return f"Error: Task '{bash_bg_id}' not found"
    return await self._kill(bash_bg_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestShellBgToolSessionIsolation -v`
Expected: PASS

- [ ] **Step 5: Run all existing ShellBgTool tests to check no regressions**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestShellBgTool -v`
Expected: Some existing tests may fail because they don't set `session_key` on meta entries. Fix them by adding `"session_key": "cli:direct"` to each `_bg_meta` entry in existing tests, OR by making the filter match tasks without `session_key` (backward compat). The recommended approach is to show tasks that have no `session_key` only when the tool's `_session_key` is the default `"cli:direct"` — this preserves backward compat for CLI.

Update `_visible_tasks`:

```python
def _visible_tasks(self) -> dict[str, dict]:
    """Return bg_meta entries belonging to the current session.

    Tasks without a session_key (created before this feature) are
    visible only when the tool is in default CLI mode.
    """
    sk = self._session_key
    return {
        k: v for k, v in _bg_meta.items()
        if v.get("session_key") == sk
        or (sk == "cli:direct" and "session_key" not in v)
    }
```

Run again: `uv run pytest tests/tools/test_shell_tool.py::TestShellBgTool -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nanobot/agent/tools/shell.py tests/tools/test_shell_tool.py
git commit -m "feat(shell): add session_key filtering to ShellBgTool"
```

---

### Task 6: Wire set_context in loop.py

**Files:**
- Modify: `nanobot/agent/loop.py:352-363` (`_set_tool_context`)

- [ ] **Step 1: Write the failing test**

Add to `tests/tools/test_shell_tool.py`:

```python
class TestLoopSetToolContext:
    """Verify _set_tool_context wires bash and shell_bg correctly."""

    def test_bash_tool_gets_context(self):
        """_set_tool_context should call set_context on bash and shell_bg tools."""
        from nanobot.agent.tools.shell import BashTool, ShellBgTool
        from nanobot.agent.tools.registry import ToolRegistry
        from unittest.mock import MagicMock

        bash = BashTool()
        bg = ShellBgTool()
        mock_bus = MagicMock()

        registry = ToolRegistry()
        registry.register(bash)
        registry.register(bg)

        # Simulate what loop.py does
        for name in ("bash", "shell_bg"):
            tool = registry.get(name)
            if tool and hasattr(tool, "set_context"):
                tool.set_context(
                    bus=mock_bus,
                    channel="feishu",
                    chat_id="chat_abc",
                    session_key="feishu:chat_abc",
                )

        assert bash._origin_channel.get() == "feishu"
        assert bash._bus is mock_bus
        assert bg._session_key == "feishu:chat_abc"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_shell_tool.py::TestLoopSetToolContext -v`
Expected: PASS — this test validates the tool interface, not loop.py itself

- [ ] **Step 3: Modify loop.py**

In `nanobot/agent/loop.py`, update `_set_tool_context()` (around line 357).

Change the tool name list from:

```python
for name in ("message", "spawn", "cron", "my"):
```

to:

```python
for name in ("message", "spawn", "cron", "my", "bash", "shell_bg"):
```

Then update the `set_context` dispatch inside the loop. Currently it handles `spawn` specially and falls through for others. Add handling for bash/shell_bg that passes bus and session_key:

```python
if name == "spawn":
    tool.set_context(channel, chat_id, effective_key=effective_key)
elif name in ("bash", "shell_bg"):
    tool.set_context(
        bus=self.bus,
        channel=channel,
        chat_id=chat_id,
        session_key=effective_key,
    )
else:
    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
```

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/tools/test_shell_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/loop.py tests/tools/test_shell_tool.py
git commit -m "feat(loop): wire set_context for bash and shell_bg tools"
```

---

### Task 7: Run full test suite and final verification

**Files:** None

- [ ] **Step 1: Run all tests**

Run: `uv run pytest tests/ -x -v`
Expected: PASS — no regressions

- [ ] **Step 2: Verify ruff linting**

Run: `uv run ruff check nanobot/agent/tools/shell.py nanobot/agent/loop.py`
Expected: No errors

- [ ] **Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: cleanup after bg task isolation implementation"
```
