# Message Queue: Per-session Pending Message Injection

## Problem

When the agent runner is processing a message (which may take minutes due to
multiple LLM calls and tool executions), any new messages from the same session
are blocked by the per-session lock (`_session_locks`). The user cannot
intervene mid-run to correct, redirect, or supplement the agent's work.

## Goal

Allow messages sent during an active runner to be injected into the runner's
context between iterations, so the LLM sees them on its next LLM call and can
adjust its behavior accordingly.

## Design

### Approach: Per-session pending queue + runner iteration callback

A lightweight callback mechanism on `AgentRunSpec` that lets the runner drain
pending user messages between iterations, without modifying the runner's core
loop structure.

### 1. Per-session pending queue in AgentLoop

**New state in `AgentLoop.__init__`:**

```python
self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
self._active_sessions: set[str] = set()
```

**Replace the session lock mechanism in `_dispatch()`:**

- Remove `_session_locks` usage.
- When `_dispatch(msg)` is called:
  - If `session_key` is in `_active_sessions` (runner is active): put the
    message into `_pending_queues[session_key]` and return immediately.
  - If not active: mark session as active, run the message through
    `_process_message()`, then drain any remaining pending messages after
    the runner finishes.
- asyncio is single-threaded, so the check-and-set on `_active_sessions` is
  atomic (no `await` between them).

**`_dispatch()` pseudocode:**

```python
async def _dispatch(self, msg: InboundMessage) -> None:
    effective_key = msg.session_key

    if effective_key in self._active_sessions:
        queue = self._pending_queues.setdefault(effective_key, asyncio.Queue())
        queue.put_nowait(msg)
        return

    self._active_sessions.add(effective_key)
    try:
        await self._process_message(msg)

        # Drain remaining pending messages after runner completes
        queue = self._pending_queues.get(effective_key)
        while queue and not queue.empty():
            batch: list[InboundMessage] = []
            while not queue.empty():
                batch.append(queue.get_nowait())
            for pending_msg in batch:
                await self._process_message(pending_msg, session_key=effective_key)
    finally:
        self._active_sessions.discard(effective_key)
```

Note: `_concurrency_gate` (semaphore) is still acquired when running
`_process_message()` but not when queuing. Pending messages don't consume
concurrency slots.

### 2. Runner callback mechanism

**New field on `AgentRunSpec`:**

```python
pending_message_callback: Callable[[], Awaitable[list[str]]] | None = None
```

**In `AgentRunner.run()`, at the top of each iteration:**

```python
for iteration in range(spec.max_iterations):
    # Drain pending user messages between iterations
    if spec.pending_message_callback:
        pending_texts = await spec.pending_message_callback()
        for text in pending_texts:
            messages.append({"role": "user", "content": text})

    # ... existing context governance, LLM call, tool execution ...
```

Messages are appended directly to the `messages` list as plain user messages.
The runner's existing context management (snipping, microcompaction) handles
them naturally since they're just regular messages in the list.

### 3. Callback construction in `_run_agent_loop()`

The callback is constructed in `_run_agent_loop()` (in `AgentLoop`), where it
has access to both the pending queue and the session:

```python
async def _drain_pending() -> list[str]:
    queue = self._pending_queues.get(session_key)
    if not queue:
        return []
    texts: list[str] = []
    while not queue.empty():
        msg = queue.get_nowait()
        texts.append(msg.content)
    # Persist to session history immediately
    if session and texts:
        for text in texts:
            session.messages.append({
                "role": "user",
                "content": text,
                "timestamp": datetime.now().isoformat(),
            })
        self.sessions.save(session)
    return texts

spec = AgentRunSpec(
    ...,
    pending_message_callback=_drain_pending if session else None,
)
```

**Key decisions:**
- Callback returns plain text strings, not `InboundMessage` objects. The runner
  doesn't need to know about message routing.
- Persistence to session history happens in the callback (session-layer concern),
  not in the runner.
- The callback is async to allow future extensions (e.g., waiting briefly to
  batch messages).

### 4. Post-runner drain

When the runner finishes and returns to `_dispatch()`, any remaining messages in
the pending queue are processed as separate `_process_message()` calls. Each
pending message starts a new runner cycle with full session context (including
previously injected messages already in session history).

No synthetic "user sent these messages" framing is added. Messages are plain
user messages. The LLM naturally understands sequential user input.

### 5. Data flow diagram

```
User sends msg_A
    ↓
AgentLoop.run() → _dispatch(msg_A)
    ↓
active_sessions.add(key)
    ↓
_process_message(msg_A) → _run_agent_loop() → AgentRunner.run()
    ↓                                               ↓
    |                                    iteration 0: LLM call → tool calls
    |                                               ↓
User sends msg_B                    iteration 1 start:
    ↓                                   callback → drain pending queue
AgentLoop.run() → _dispatch(msg_B)        → returns ["msg_B content"]
    ↓                                   messages.append(user: "msg_B content")
key in active_sessions? YES           LLM sees msg_A context + msg_B
    ↓                                   → adjusts behavior accordingly
pending_queue[key].put(msg_B)
    ↓
return immediately (no blocking)
```

### 6. Edge cases

**Tool execution timing:** Messages arriving during `_execute_tools()` are
queued and injected at the next iteration start, after tool results are
processed. This is the correct behavior — the LLM sees both the tool results
and the new user input together.

**Queue growth:** No size limit on pending queues. In normal use, users won't
send hundreds of messages while waiting. If needed, a `maxsize` can be added
later.

**Stream compatibility:** Injecting messages between iterations doesn't affect
streaming. Streaming is per-LLM-call, and injection happens between calls.

**/stop command:** The existing `/stop` mechanism cancels the active task. After
cancellation, `_active_sessions.discard()` runs in the `finally` block, and any
pending messages can be processed by subsequent dispatches.

## Files to modify

| File | Change |
|------|--------|
| `nanobot/agent/loop.py` | Replace `_session_locks` with `_pending_queues` + `_active_sessions`; modify `_dispatch()`; construct callback in `_run_agent_loop()` |
| `nanobot/agent/runner.py` | Add `pending_message_callback` field to `AgentRunSpec`; call it at iteration start in `AgentRunner.run()` |

## Out of scope

- Interrupting/cancelling the runner mid-iteration (could be a future enhancement)
- Cross-session message routing changes
- Rate limiting on pending messages
