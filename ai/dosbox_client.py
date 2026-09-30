"""
DOSBoxClient: Python client for the native DOSBox-X AI bridge
(dosbox-src/src/debug/debug_ai.cpp), connecting over TCP to
127.0.0.1:9876 using newline-delimited JSON (see
docs/dosbox-ai-bridge.md).

This replaces FakeDOSBoxDebugger for production MCP tool execution.
FakeDOSBoxDebugger remains available (ai/debugger.py) for unit tests that
don't require a live DOSBox-X instance.
"""

import itertools
import json
import socket
from typing import Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 5.0


class DOSBoxClientError(Exception):
    """Base class for all DOSBoxClient errors. `code` is a stable,
    machine-readable error code -- for errors that originated as a native
    bridge error response, this is always the exact code the bridge sent
    (e.g. "INVALID_PARAMETER"), even when the exception class itself is
    the generic DOSBoxProtocolError catch-all. Never collapse `code` away
    when reporting an error to a caller -- it's the specific, actionable
    part of the message."""

    code: str = "DOSBOX_CLIENT_ERROR"


class DOSBoxNotConnected(DOSBoxClientError):
    """Could not connect to, or lost connection to, the DOSBox-X AI bridge."""

    code = "DOSBOX_NOT_CONNECTED"


class DOSBoxTimeout(DOSBoxClientError):
    """The DOSBox-X AI bridge did not respond within the request timeout."""

    code = "DOSBOX_TIMEOUT"


class DOSBoxProtocolError(DOSBoxClientError):
    """The DOSBox-X AI bridge returned malformed data, or a native error
    code this client does not have a more specific exception class for
    (e.g. INVALID_PARAMETER, INVALID_REQUEST, UNKNOWN_METHOD, INVALID_JSON).
    `code` is set to the exact native error code when there was one."""

    code = "DOSBOX_PROTOCOL_ERROR"


class DOSBoxDebuggerNotStopped(DOSBoxClientError):
    """The DOSBox-X debugger is not currently active, so the request could
    not be serviced (see native error code DEBUGGER_NOT_STOPPED)."""

    code = "DEBUGGER_NOT_STOPPED"


class DOSBoxMemoryError(DOSBoxClientError):
    """The requested guest memory is unmapped or inaccessible (see native
    error code MEMORY_ERROR)."""

    code = "MEMORY_ERROR"


class DOSBoxRegisterNotWritable(DOSBoxClientError):
    """The requested register is not in the Phase 4A write whitelist (see
    native error code REGISTER_NOT_WRITABLE). EIP, segment registers, ESP,
    and EFLAGS are deliberately protected -- only EAX/EBX/ECX/EDX/ESI/EDI/
    EBP can be written."""

    code = "REGISTER_NOT_WRITABLE"


class DOSBoxBreakpointNotFound(DOSBoxClientError):
    """No breakpoint exists with the given id (see native error code
    BREAKPOINT_NOT_FOUND). Breakpoint ids are positions in DOSBox-X's own
    breakpoint list (the same ids the debugger GUI's BPLIST/BPDEL commands
    use) and shift when breakpoints are added/removed -- re-run
    list_breakpoints() to get current ids before deleting."""

    code = "BREAKPOINT_NOT_FOUND"


class DOSBoxBreakpointAlreadyExists(DOSBoxClientError):
    """A breakpoint already exists at the requested address (see native
    error code BREAKPOINT_ALREADY_EXISTS). This is a bridge-level policy
    choice, not a DOSBox-X debugger limitation -- see
    docs/dosbox-ai-bridge.md."""

    code = "BREAKPOINT_ALREADY_EXISTS"


class DOSBoxAlreadyRunning(DOSBoxClientError):
    """continue_execution()/step_into()/step_over() was called while guest
    execution is already running -- the debugger is not currently stopped
    (see native error code ALREADY_RUNNING, Phase 4C/4D). Call
    get_debug_status() first if unsure of the current state."""

    code = "ALREADY_RUNNING"


class DOSBoxAlreadyStopped(DOSBoxClientError):
    """pause_execution() was called while the debugger is already stopped
    (see native error code ALREADY_STOPPED, Phase 4C). Call
    get_debug_status() first if unsure of the current state."""

    code = "ALREADY_STOPPED"


class DOSBoxExecutionTimeout(DOSBoxClientError):
    """pause_execution() or step_over() was requested but did not complete
    within the native bridge's timeout (see native error code
    EXECUTION_TIMEOUT, Phase 4C/4D). For step_over(), this means the
    stepped-over call/int/loop/rep instruction had not returned within the
    timeout -- it does not necessarily mean the step failed; call
    get_debug_status() to check whether it has completed since."""

    code = "EXECUTION_TIMEOUT"


_NATIVE_ERROR_MAP = {
    "DEBUGGER_NOT_STOPPED": DOSBoxDebuggerNotStopped,
    "MEMORY_ERROR": DOSBoxMemoryError,
    "REGISTER_NOT_WRITABLE": DOSBoxRegisterNotWritable,
    "BREAKPOINT_NOT_FOUND": DOSBoxBreakpointNotFound,
    "BREAKPOINT_ALREADY_EXISTS": DOSBoxBreakpointAlreadyExists,
    "ALREADY_RUNNING": DOSBoxAlreadyRunning,
    "ALREADY_STOPPED": DOSBoxAlreadyStopped,
    "EXECUTION_TIMEOUT": DOSBoxExecutionTimeout,
}


class DOSBoxClient:
    """Thin client for the native AI bridge. Not thread-safe -- each MCP
    server process should use one client from one thread at a time, matching
    the bridge's one-in-flight-request-per-connection design."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._id_counter = itertools.count(1)

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as e:
            raise DOSBoxNotConnected(
                f"could not connect to the DOSBox-X AI bridge at {self.host}:{self.port}: {e}"
            ) from e
        sock.settimeout(self.request_timeout)
        self._sock = sock
        self._buf = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def __enter__(self) -> "DOSBoxClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _read_line(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as e:
                raise DOSBoxTimeout(
                    f"the DOSBox-X AI bridge at {self.host}:{self.port} did not respond in time"
                ) from e
            except OSError as e:
                self.close()
                raise DOSBoxNotConnected(f"lost connection to the DOSBox-X AI bridge: {e}") from e
            if not chunk:
                self.close()
                raise DOSBoxNotConnected("the DOSBox-X AI bridge closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def request(self, method: str, params: Optional[dict] = None) -> object:
        """Send one request and return its "result" on success. Raises a
        DOSBoxClientError subclass on any failure -- callers never see raw
        socket exceptions or malformed data."""

        if self._sock is None:
            self.connect()

        request_id = next(self._id_counter)
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            self._sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError as e:
            self.close()
            raise DOSBoxNotConnected(f"lost connection to the DOSBox-X AI bridge: {e}") from e

        line = self._read_line()

        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DOSBoxProtocolError(f"malformed JSON response from the DOSBox-X AI bridge: {e}") from e

        if not isinstance(response, dict) or "ok" not in response:
            raise DOSBoxProtocolError(f"malformed response from the DOSBox-X AI bridge: {response!r}")

        if response.get("id") != request_id:
            raise DOSBoxProtocolError(
                f"response id {response.get('id')!r} does not match request id {request_id!r}"
            )

        if not response["ok"]:
            error = response.get("error") or {}
            code = error.get("code", "UNKNOWN_ERROR")
            message = error.get("message", "no message")
            exc_class = _NATIVE_ERROR_MAP.get(code, DOSBoxProtocolError)
            exc = exc_class(f"{code}: {message}")
            exc.code = code  # always the exact native code, even under the generic catch-all
            raise exc

        return response.get("result")

    # -- high-level typed methods, matching the native bridge's Phase 3B API --

    def get_debug_status(self) -> dict:
        return self.request("debug.status")

    def get_cpu_state(self) -> dict:
        return self.request("cpu.get")

    def read_memory(self, address: str, length: int) -> dict:
        return self.request("memory.read", {"address": address, "length": length})

    def get_current_instruction(self) -> dict:
        return self.request("code.current")

    def disassemble(self, address: str, count: int) -> list:
        return self.request("code.disassemble", {"address": address, "count": count})

    def write_memory(self, address: str, data: list) -> dict:
        """Write bytes (list of ints 0-255 and/or 2-digit hex strings, e.g.
        [0xB8, "34", "12"]) starting at a "SEG:OFF" address."""

        return self.request("memory.write", {"address": address, "data": list(data)})

    def write_register(self, register: str, value) -> dict:
        """Write one general-purpose register (eax/ebx/ecx/edx/esi/edi/ebp
        only -- EIP, segment registers, ESP, and EFLAGS are rejected by the
        native bridge with DOSBoxRegisterNotWritable). `value` may be an
        int or an up-to-8-digit hex string."""

        if isinstance(value, int):
            if not (0 <= value <= 0xFFFFFFFF):
                raise ValueError(f"register value out of 32-bit range: {value!r}")
            value_str = f"{value:08X}"
        elif isinstance(value, str):
            value_str = value
        else:
            raise TypeError(f"register value must be int or str, got {type(value).__name__}")

        return self.request("register.write", {"register": register, "value": value_str})

    def set_breakpoint(self, address: str) -> dict:
        """Set a breakpoint at a "SEG:OFF" address, using DOSBox-X's own
        breakpoint mechanism (CBreakpoint/BPoints -- the same one the
        debugger GUI's BP command uses). Raises DOSBoxBreakpointAlreadyExists
        if a breakpoint already exists at that address."""

        return self.request("breakpoint.set", {"address": address})

    def delete_breakpoint(self, breakpoint_id: int) -> dict:
        """Delete a breakpoint by id (as returned by set_breakpoint/
        list_breakpoints). Ids are positions in DOSBox-X's own breakpoint
        list and shift when breakpoints are added/removed -- call
        list_breakpoints() again if unsure an id is still current. Raises
        DOSBoxBreakpointNotFound if no breakpoint has that id."""

        return self.request("breakpoint.delete", {"id": breakpoint_id})

    def list_breakpoints(self) -> list:
        """List every breakpoint in DOSBox-X's own breakpoint list, each
        tagged with its `type` (physical, log, memory, memory_linear,
        interrupt)."""

        result = self.request("breakpoint.list")
        return result.get("breakpoints", []) if isinstance(result, dict) else []

    # -- Phase 6 (docs/phase6-watch-int-log-breakpoints.md) ---------------

    def set_memory_breakpoint(self, address: Optional[str] = None, linear: Optional[str] = None,
                              length: int = 1) -> dict:
        """Stop when any of `length` (1-16) bytes changes, using DOSBox-X's
        own memory breakpoints (BPM / BPLM). Give exactly one of `address`
        ("SEG:OFF") or `linear` (hex). When one fires, get_debug_status()
        carries `last_memory_hit` (address, old, new) until the next
        continue. Needs a --enable-debug=heavy build (NOT_SUPPORTED
        otherwise)."""

        if (address is None) == (linear is None):
            raise ValueError("give exactly one of address or linear")
        params: dict = {"length": length}
        if address is not None:
            params["address"] = address
        else:
            params["linear"] = linear
        return self.request("breakpoint.set_memory", params)

    def set_interrupt_breakpoint(self, interrupt: int, ah: Optional[int] = None,
                                 al: Optional[int] = None) -> dict:
        """Stop before `INT interrupt` runs, optionally only for a given AH
        (and AL). Example: interrupt=0x21, ah=0x4B stops on DOS EXEC."""

        params: dict = {"int": interrupt}
        if ah is not None:
            params["ah"] = ah
        if al is not None:
            params["al"] = al
        return self.request("breakpoint.set_interrupt", params)

    def set_log_breakpoint(self, address: str, stack_words: int = 0) -> dict:
        """A breakpoint that records registers, segments and `stack_words`
        (0-32) words at SS:SP, then resumes on its own. Read the records
        with read_log()."""

        return self.request("breakpoint.set_log", {"address": address, "stack_words": stack_words})

    def read_log(self, max_entries: int = 1000, clear: bool = True) -> dict:
        """Return {"entries", "remaining", "dropped"} from the logging-
        breakpoint buffer (newest 4096 kept). Works while the guest runs."""

        return self.request("log.read", {"max": max_entries, "clear": clear})

    def continue_execution(self) -> dict:
        """Resume real guest CPU execution -- the SAME transition the
        debugger GUI's "RUN" command makes (DEBUG_Run()/
        DOSBOX_SetNormalLoop()). Only valid while the debugger is stopped;
        raises DOSBoxAlreadyRunning (native ALREADY_RUNNING) otherwise.
        Returns {"stopped": False, "running": True} -- the guest CPU is
        genuinely running by the time this call returns; it does not wait
        for (or fabricate) a subsequent breakpoint hit."""

        return self.request("execution.continue")

    def pause_execution(self) -> dict:
        """Stop real guest CPU execution and enter the debugger -- the SAME
        transition Ctrl+Pause makes (DEBUG_Enable_Handler()). Only valid
        while guest code is running; raises DOSBoxAlreadyStopped (native
        ALREADY_STOPPED) if the debugger is already stopped. Blocks (on the
        native bridge side) until the pause has genuinely happened -- the
        returned dict is a real debug.status snapshot (location,
        instruction, registers, segments, flags) taken after the CPU
        actually stopped, not merely an acknowledgement that the request
        was received. Raises DOSBoxExecutionTimeout (native
        EXECUTION_TIMEOUT) if it didn't complete in time."""

        return self.request("execution.pause")

    def step_into(self) -> dict:
        """Execute exactly one guest instruction -- the SAME transition the
        debugger GUI's F11 ("trace into") key makes (DEBUG_Run(1,true)).
        Only valid while the debugger is stopped; raises DOSBoxAlreadyRunning
        (native ALREADY_RUNNING) otherwise. Always synchronous: blocks until
        the real DOSBox-X CPU decoder has genuinely executed the instruction,
        and returns a real debug.status snapshot (location, instruction,
        registers, segments, flags) taken after it stopped again -- the
        same shape pause_execution() returns."""

        return self.request("execution.step_into")

    def step_over(self) -> dict:
        """Step over the current instruction -- the SAME transition the
        debugger GUI's F10 ("step over") key makes (StepOver()+DEBUG_Run()).
        For an ordinary instruction this behaves exactly like step_into().
        For a call/int/loop/rep instruction, DOSBox-X's own StepOver()
        places a one-shot breakpoint just past it and lets the subroutine
        run for real; this call blocks (on the native bridge side) until
        that breakpoint (or any other event) re-enters the debugger, then
        returns the real post-step debug.status snapshot, exactly like
        step_into(). Only valid while the debugger is stopped; raises
        DOSBoxAlreadyRunning (native ALREADY_RUNNING) otherwise. Raises
        DOSBoxExecutionTimeout (native EXECUTION_TIMEOUT) if the
        stepped-over call/int/loop/rep does not return within the
        timeout -- call get_debug_status() afterward to check whether it
        completed shortly after."""

        return self.request("execution.step_over")
