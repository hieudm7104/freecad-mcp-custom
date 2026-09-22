"""GUI-thread task dispatch for the RPC server.

The XML-RPC server runs in its own thread. FreeCAD APIs that touch the GUI
or the document tree must run in the main GUI thread. This module owns the
queue that ferries wrapped callables onto the GUI thread and the helper that
RPC handlers use to invoke them.

Robustness and performance guarantees:

1. Per-call response queues: each ``dispatch_to_gui`` call owns its own
   ``queue.Queue``. A timeout in one call can never corrupt the response for
   a subsequent call.
2. Immediate wake via Qt signal: ``dispatch_to_gui`` emits a signal from the
   RPC thread; the GUI thread processes the task immediately rather than
   waiting for the next 500 ms heartbeat tick. The 500 ms heartbeat is kept
   only as a fallback.
3. Mouse-button guard: ``process_gui_tasks`` skips the current tick while
   mouse buttons are held so MCP tasks cannot interrupt 3D navigation drags.
4. Clean shutdown: ``stop_rpc_server`` calls ``stop_heartbeat`` directly; the
   ``_SHUTDOWN`` sentinel stops a drain that is already in flight.
5. Exception isolation: exceptions inside a task are caught, logged, and
   returned as error strings; they never kill the dispatch loop.
6. Stuck-task fail-fast: once a task that already started times out, later GUI
   calls fail immediately until that task returns. Status remains available
   through a GUI-independent RPC method.
7. Timeout counts from task start: the GUI thread runs queued tasks one at a
   time, so a call that arrives while another task is running waits in the
   FIFO first. That wait is budgeted separately (``queue_timeout``) and does
   not consume the task's own ``timeout``. Two concurrent ``execute_code``
   calls therefore each get their full run budget instead of the second one
   being reported stuck because the first was slow.
"""

import itertools
import queue
import threading
import time
import traceback
from typing import Any, Callable

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtWidgets

from rpc_server.dispatch_health import DispatchHealth, stuck_failure


_rpc_request_queue: "queue.Queue[Any]" = queue.Queue()
_SHUTDOWN = object()
_processing = False  # re-entrancy guard: True while process_gui_tasks is draining
_processing_since: float = 0.0  # wall-clock time when _processing became True
_task_ids = itertools.count(1)
_dispatch_health = DispatchHealth()
_heartbeat: "QtCore.QTimer | None" = None
# Mirrors _heartbeat for status reads. get_dispatch_status() runs on the RPC
# thread — that is the whole point of it, it has to answer when the GUI is
# wedged — and QTimer.isActive() is a cross-thread Qt call from there.
_heartbeat_on = False
# Why the pump last refused to drain, and since when. A deferral is normal for
# a fraction of a second (a drag, a menu); one that never clears means the
# queue is dead, which used to be invisible because get_rpc_status reported
# only _dispatch_health -- and that stays "healthy" when no task ever STARTS.
_blocked_by: str | None = None
_blocked_since: float = 0.0
_blocked_logged = False


class _WakeSignal(QtCore.QObject):
    """Qt signal bridge for cross-thread GUI-task wakeup.

    Must be created on the GUI thread (``init_waker``). Emitting from the
    RPC thread is safe: Qt delivers the connection with ``QueuedConnection``,
    so the slot always fires in the GUI thread's event loop.
    """
    _sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._sig.connect(self._on_wake, QtCore.Qt.QueuedConnection)

    def wake(self) -> None:
        self._sig.emit()

    def _on_wake(self) -> None:
        process_gui_tasks()


_waker: "_WakeSignal | None" = None


def init_waker() -> None:
    """Create the wake-signal bridge. Call once from the GUI thread."""
    global _waker
    _waker = _WakeSignal()


def cleanup_waker() -> None:
    """Release the wake-signal bridge on server stop."""
    global _waker
    _waker = None


def start_heartbeat(interval_ms: int = 500) -> None:
    """Start the fallback drain timer. Call once from the GUI thread.

    A repeating QTimer, not a chain of ``singleShot`` calls. The chain had a
    single point of failure: ``process_gui_tasks`` returns early when
    ``_processing`` is set, and that ``return`` sits *above* the ``try`` whose
    ``finally`` armed the next tick. A tick delivered by ``processEvents()``
    inside a running task was therefore swallowed without re-arming, and when
    the drain it interrupted was the wake path (``reschedule=False``, which
    never arms one either) the chain ended for good -- silently, because the
    Qt event loop keeps running and nothing logs. A repeating timer cannot be
    lost: a swallowed tick is just a skipped tick.
    """
    global _heartbeat, _heartbeat_on
    if _heartbeat is not None:
        return
    # A stop() leaves its _SHUTDOWN sentinel in the queue: stop_rpc_server posts
    # it and then stops the timer, so no tick ever consumes it. Left there, the
    # first tick after a restart would drain it and stop the pump again —
    # server "started", queue dead.
    kept = []
    while True:
        try:
            item = _rpc_request_queue.get_nowait()
        except queue.Empty:
            break
        if item is not _SHUTDOWN:
            kept.append(item)  # the RPC thread is already accepting: keep real work
    for item in kept:
        _rpc_request_queue.put(item)
    _heartbeat = QtCore.QTimer()
    _heartbeat.setInterval(interval_ms)
    _heartbeat.timeout.connect(process_gui_tasks)
    _heartbeat.start()
    _heartbeat_on = True


def stop_heartbeat() -> None:
    global _heartbeat, _heartbeat_on
    _heartbeat_on = False
    if _heartbeat is not None:
        _heartbeat.stop()
        _heartbeat = None


def _flush_gui_events(delay_ms: int = 20) -> None:
    FreeCADGui.updateGui()
    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    # ExcludeUserInputEvents: skip mouse/keyboard events to avoid re-entrancy
    # with ongoing navigation. ExcludeSocketNotifiers keeps network I/O out.
    flags = (
        QtCore.QEventLoop.ExcludeUserInputEvents
        | QtCore.QEventLoop.ExcludeSocketNotifiers
    )
    app.processEvents(flags, delay_ms)
    if delay_ms > 0:
        QtCore.QThread.msleep(delay_ms)
        app.processEvents(flags, delay_ms)


def process_gui_tasks(reschedule: bool = True) -> None:
    """Drain queued GUI-thread callables.

    Skips the current tick when any mouse button is held (e.g., 3D navigation
    drag) or when already executing a task (re-entrancy guard). The guard
    prevents ``doc.recompute()`` or ``processEvents()`` inside a task from
    triggering a nested ``process_gui_tasks`` call that corrupts FreeCAD state.

    ``reschedule`` is accepted and ignored; the heartbeat is now a repeating
    timer owned by ``start_heartbeat`` (see there for why the old self-arming
    chain was a liability). Kept in the signature so an out-of-tree caller
    passing it keeps working.
    """
    global _processing, _processing_since
    if _processing:
        return  # re-entrant call from processEvents inside a task; skip

    shutdown = False
    try:
        if _rpc_request_queue.empty():
            _note_unblocked()
            return  # nothing queued; skip cursor/status-bar churn on idle heartbeat ticks
        # Each of these defers the tick. On a desktop they clear the moment the
        # user lets go; in this headless container nobody ever will, so a
        # modal dialog opened by generated code wedges the queue permanently.
        # Record which one, so get_rpc_status can say so instead of reporting
        # "healthy" while nothing drains.
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            _note_blocked("mouse_button_held")
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            _note_blocked("popup_open")
            return
        if QtWidgets.QApplication.activeModalWidget() is not None:
            _note_blocked("modal_dialog_open")
            return
        _note_unblocked()

        _processing = True
        _processing_since = time.monotonic()
        app = QtWidgets.QApplication.instance()
        try:
            status_bar = FreeCADGui.getMainWindow().statusBar()
        except Exception:
            status_bar = None

        if app is not None:
            app.setOverrideCursor(QtCore.Qt.WaitCursor)
        if status_bar is not None:
            status_bar.showMessage("MCP: processing…")
        try:
            while not _rpc_request_queue.empty():
                task = _rpc_request_queue.get()
                if task is _SHUTDOWN:
                    shutdown = True
                    return
                try:
                    task()
                except Exception as e:
                    FreeCAD.Console.PrintError(
                        f"MCP RPC: unhandled exception in GUI task: {type(e).__name__}: {e}\n"
                        f"{traceback.format_exc()}"
                    )
        finally:
            if app is not None:
                app.restoreOverrideCursor()
            if status_bar is not None:
                status_bar.clearMessage()
    finally:
        _processing = False
        if shutdown:
            stop_heartbeat()


def request_shutdown() -> None:
    """Post the sentinel so the next dispatch tick exits without rescheduling."""
    _rpc_request_queue.put(_SHUTDOWN)


def _note_blocked(reason: str) -> None:
    global _blocked_by, _blocked_since, _blocked_logged
    now = time.monotonic()
    if _blocked_by != reason:
        _blocked_by, _blocked_since, _blocked_logged = reason, now, False
    elif not _blocked_logged and now - _blocked_since > 30:
        _blocked_logged = True
        FreeCAD.Console.PrintError(
            f"MCP RPC: GUI queue has not drained for {now - _blocked_since:.0f}s "
            f"({reason}). Nothing here can dismiss it -- restart the FreeCAD "
            f"container. Generated code must never open a modal dialog.\n"
        )


def _note_unblocked() -> None:
    global _blocked_by, _blocked_logged
    if _blocked_by is not None:
        _blocked_by, _blocked_logged = None, False


def get_dispatch_status() -> dict[str, Any]:
    """Return GUI dispatch health without touching FreeCAD's GUI thread."""
    status = _dispatch_health.snapshot()
    # _dispatch_health only tracks tasks that STARTED, so on its own it reports
    # "healthy" for a queue that never drains at all -- which is exactly the
    # failure that is hardest to notice. Report the pump separately.
    status["pump"] = {
        "heartbeat": _heartbeat_on,
        "queued": _rpc_request_queue.qsize(),
        "draining": _processing,
        "blocked_by": _blocked_by,
        "blocked_for_seconds": (
            round(time.monotonic() - _blocked_since, 1) if _blocked_by else 0.0
        ),
    }
    return status


def dispatch_to_gui(
    task: Callable[[], Any],
    timeout: float = 60,
    operation_name: str | None = None,
    queue_timeout: float | None = None,
) -> Any:
    """Run ``task`` on the GUI thread and return its result.

    Uses a per-call response queue so a timeout in one call never corrupts
    the response for a subsequent call. Wakes the GUI thread immediately via
    a Qt signal instead of waiting for the next 500 ms heartbeat.

    ``timeout`` is the run budget and starts counting when the task actually
    begins on the GUI thread. Time spent queued behind earlier tasks is
    budgeted separately by ``queue_timeout`` (defaults to ``timeout``); if the
    task has not started by then it is cancelled without marking dispatch
    stuck. A call that is already queued when an earlier task becomes stuck
    keeps waiting for its turn; only calls arriving after that point are
    rejected immediately.

    A task already running on the GUI thread cannot be interrupted; if it
    exceeds ``timeout`` it is marked as stuck and subsequent GUI calls fail
    immediately until it returns.

    Returns the task's return value on success, an error string if the task
    raises, or ``{"success": False, "error": ...}`` on timeout.
    """
    rejection = _dispatch_health.rejection()
    if rejection is not None:
        return rejection

    if queue_timeout is None:
        queue_timeout = timeout

    task_id = next(_task_ids)
    operation = operation_name or getattr(task, "__name__", "GUI operation")
    if operation == "<lambda>":
        operation = "GUI operation"

    response_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1)
    state_lock = threading.Lock()
    started_event = threading.Event()
    started_at: float | None = None
    cancelled = False

    def _wrapped() -> None:
        nonlocal started_at
        with state_lock:
            if cancelled:
                return  # caller timed out and went away; don't run a stale task
            started_at = time.monotonic()
            _dispatch_health.start(task_id, operation)
            started_event.set()
        missing = object()
        res = missing
        try:
            try:
                res = task()
            except Exception as e:
                FreeCAD.Console.PrintError(
                    f"MCP RPC: GUI task raised {type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )
                res = f"{type(e).__name__}: {e}"
        finally:
            # Publish completion atomically with clearing health, so a deadline
            # racing with completion cannot report a missing successful result.
            with state_lock:
                _dispatch_health.finish(task_id)
                if res is not missing:
                    response_queue.put_nowait(res)

    queued_at = time.monotonic()
    _rpc_request_queue.put(_wrapped)
    if _waker is not None:
        _waker.wake()  # immediate wake via Qt signal (thread-safe)

    # Phase 1: wait for the task to start. Earlier queued tasks run first on
    # the GUI thread; that wait must not eat into this task's run budget.
    queue_deadline = queued_at + queue_timeout
    if not started_event.wait(max(0, queue_deadline - time.monotonic())):
        with state_lock:
            cancelled = started_at is None
    if cancelled:
        queued_for = time.monotonic() - queued_at
        if _processing:
            busy_for = time.monotonic() - _processing_since
            hint = (
                f" (GUI thread has been busy for {busy_for:.1f}s — for heavy OCCT"
                " geometry consider execute_code_async, which must apply document"
                " writes through its commit() helper)"
            )
        else:
            hint = ""
        return {
            "success": False,
            "error": (
                f"GUI dispatch gave up after {queued_for:.1f}s waiting for "
                f"'{operation}' to start (queue_timeout={queue_timeout:g}s){hint}"
            ),
        }

    # Phase 2: count from actual GUI start, even if this RPC thread woke late.
    assert started_at is not None
    run_remaining = max(0, started_at + timeout - time.monotonic())
    try:
        return response_queue.get(timeout=run_remaining)
    except queue.Empty:
        with state_lock:
            # Completion may have won the race while we acquired the lock.
            try:
                return response_queue.get_nowait()
            except queue.Empty:
                stuck = _dispatch_health.mark_timed_out(task_id, timeout)
                if stuck is not None:
                    return stuck_failure(stuck, just_timed_out=True)
                return {"success": False, "error": f"GUI dispatch timed out after {timeout}s"}
