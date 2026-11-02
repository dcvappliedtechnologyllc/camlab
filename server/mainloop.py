"""Main-thread job runner.

Canon's EDSDK on macOS only discovers cameras (and delivers its events) on
the process main thread, bench-verified 2026-10-25: the same code finds the
R50 V in ~11 s on the main thread and never on a worker thread. So the
server keeps the main thread as a small job loop: uvicorn (and MCP stdio)
run in background threads, and anything that must run on main is submitted
here. Idle callbacks (the EDSDK event pump) run between jobs.

Standalone scripts need no loop: `submit()` from the main thread while no
loop is running just calls the function inline.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger("camlab.mainloop")


class MainLoop:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._idle: list[Callable[[], None]] = []
        self.running = False
        self.thread: threading.Thread | None = None

    def register_idle(self, fn: Callable[[], None]) -> None:
        if fn not in self._idle:
            self._idle.append(fn)

    def unregister_idle(self, fn: Callable[[], None]) -> None:
        if fn in self._idle:
            self._idle.remove(fn)

    def submit(self, fn: Callable[..., Any], *args: Any, timeout: float = 120) -> Any:
        """Run fn(*args) on the main thread and return its result."""
        on_main = threading.current_thread() is threading.main_thread()
        if on_main and (not self.running or self.thread is threading.current_thread()):
            return fn(*args)
        if not self.running:
            raise RuntimeError("main loop is not running; EDSDK work must be submitted from the main thread")
        box: queue.Queue = queue.Queue()
        self._q.put((fn, args, box))
        try:
            ok, val = box.get(timeout=timeout)
        except queue.Empty as e:
            raise TimeoutError(f"main-thread job timed out: {getattr(fn, '__name__', fn)}") from e
        if ok:
            return val
        raise val

    def run(self, should_stop: Callable[[], bool], idle_s: float = 0.02) -> None:
        """Serve jobs on the calling (main) thread until should_stop()."""
        self.running = True
        self.thread = threading.current_thread()
        try:
            while not should_stop():
                try:
                    fn, args, box = self._q.get(timeout=idle_s)
                except queue.Empty:
                    for cb in list(self._idle):
                        try:
                            cb()
                        except Exception as e:  # noqa: BLE001
                            log.debug("idle callback: %s", e)
                    continue
                try:
                    box.put((True, fn(*args)))
                except BaseException as e:  # noqa: BLE001
                    box.put((False, e))
        finally:
            self.running = False
            self.thread = None

    # ---- Cocoa mode (Mac app): the NSApplication run loop owns the main thread,
    # so instead of run() a timer on that run loop calls tick() every ~20 ms.
    def begin_external(self) -> None:
        """Mark the loop as served externally (by tick() from a run-loop timer on main)."""
        self.running = True
        self.thread = threading.current_thread()

    def end_external(self) -> None:
        self.running = False
        self.thread = None

    def tick(self, max_jobs: int = 50) -> int:
        """Drain queued jobs (up to max_jobs) and run idle callbacks once. Call on the main thread."""
        n = 0
        while n < max_jobs:
            try:
                fn, args, box = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                box.put((True, fn(*args)))
            except BaseException as e:  # noqa: BLE001
                box.put((False, e))
            n += 1
        for cb in list(self._idle):
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log.debug("idle callback: %s", e)
        return n


LOOP = MainLoop()
