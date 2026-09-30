# Phase 6 — memory, interrupt and logging breakpoints

Status: design + implementation on branch `feat/bridge-watch-int-log`
(submodule branch `ai-bridge-watch-int-log`). Not pushed.

## 1. Why

The first real investigation through the bridge (EOB II Chinese `START.EXE`,
comparing DOSBox-X against another emulator) hit three gaps:

| Gap | Cost in that session |
|---|---|
| No way to stop when a program is loaded | `-break-start` stops at `F000:FFF0`; the load segment had to be found in a first run, the breakpoint set in a second |
| Every breakpoint hit is a stop + client round trip | ~260 allocator hits took minutes; each hit needed `debug.status`, `cpu.get`, `memory.read`, `execution.continue` |
| No memory-write breakpoint | the root cause (an overlay stub field that should be cleared) needs "who writes this byte" |

DOSBox-X's native debugger already has all three mechanisms (`BPM`/`BPLM`,
`BPINT`, and the physical breakpoint used by the bridge). This phase exposes
them; it does not add a second breakpoint store.

## 2. New bridge methods

All go through `CBreakpoint::BPoints`, the same list the GUI commands use.

### `breakpoint.set_memory`

```json
{"address": "SEG:OFF", "length": 1}
{"linear": "3DE70", "length": 2}
```

- One native memory breakpoint per byte (`AddMemBreakpoint`; `linear` uses
  `BKPNT_MEMORY_LINEAR`), `length` 1–16.
- The reference value is initialised to the byte's current contents, so the
  first check does not fire spuriously (the GUI's `BPM` starts from 0).
- Fires when the byte **changes** (native semantics: value compare after each
  instruction, heavy debugger only). Without `C_HEAVY_DEBUG` the method
  returns `NOT_SUPPORTED`.
- Result: `{"ids":[...], "values":["E6",...]}`.
- When one fires, `debug.status` carries
  `"last_memory_hit":{"address":"SEG:OFF","linear":"XXXXX","old":"E6","new":"00"}`
  until the next `execution.continue`.

### `breakpoint.set_interrupt`

```json
{"int": 33}                 // any AH/AL
{"int": 33, "ah": 75}       // INT 21h AH=4Bh (EXEC)
{"int": 33, "ah": 75, "al": 0}
```

Native `AddIntBreakpoint`; stops before the interrupt runs.

### `breakpoint.set_log`

```json
{"address": "SEG:OFF", "stack_words": 8}
```

A physical breakpoint that does not hand control to the client: when the
debugger stops on it, the bridge records registers, segments and
`stack_words` (0–32) words at `SS:SP`, then resumes by the same
`DEBUG_AI_DoContinue()` as `execution.continue`. A normal client pause that
happens to land on the same address is also auto-resumed; do not mix the two
at one address.

### `log.read`

```json
{"max": 1000, "clear": true}
```

Returns `{"entries":[...], "dropped": n}`. The buffer keeps the newest 4096
entries; older ones are counted in `dropped`. Each entry:
`{"seq", "address", "registers", "segments", "stack":["XXXX",...]}`.

### `breakpoint.list`

Now lists every breakpoint with a `type`: `physical`, `log`, `memory`,
`memory_linear`, `interrupt` (plus `int`/`ah`/`al` for interrupts).

## 3. MCP / client surface

`ai/dosbox_client.py`: `set_memory_breakpoint`, `set_interrupt_breakpoint`,
`set_log_breakpoint`, `read_log`. The same four are registered as MCP tools
in `ai/server.py` only. The Phase 5A/5C servers and
`tests/phase5a/agent_cli.py` `TOOL_DESCRIPTIONS` are frozen evaluation tool
surfaces (5C already exposes fewer tools than `server.py`, and
`tests/phase5b/scenarios.py` derives `FULL_TOOL_SET` from them), so they are
left unchanged; adding tools there would change what those scenarios grade.

## 4. Verification

- Unit: Python client request shapes (no emulator).
- Real: the EOB II case — `set_interrupt` on `INT 21h AH=4Bh` lands on the
  program load in one run; `set_log` on the allocator returns all hits in one
  `log.read`; `set_memory` on the overlay stub load-segment field reports the
  writer.

Results (2026-09-30, heavy-debug build in a Linux container):

- `set_interrupt` on `INT 21h AH=4Ah` stopped on every resize; the program's
  own one was picked by checking that `CS:0000` is the EXE's first byte.
- `set_memory` on the EOB II overlay stub (`336B:0000`–`0011`) reported 8
  hits in one run, each with writer `CS:IP`, old and new byte; the load
  segment field went `0 → 43DF → 44FA → 0 → 46EF`.
- `set_log` on the allocator entry (`26C2:0001`, 6 stack words) collected 260
  records with 0 dropped, read in one `log.read` while the guest ran.
- `breakpoint.list` returned all three kinds with their `type`.
- Python: `tests/test_debugger.py`, `tests/phase5b`, `tests/phase5c` — 74
  passed, 6 skipped (need a live emulator). `ai/server.py` lists 20 tools
  including the four new ones.
