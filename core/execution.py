"""Coordinate cancellation with file publication and external experiment clients."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import subprocess
import threading
import time
from typing import Iterator, Optional


class ExecutionCancelled(TimeoutError):
    pass


@dataclass
class ExecutionControl:
    seconds: Optional[float] = None
    _started: float = field(default_factory=time.monotonic)
    cancellation: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _processes: set = field(default_factory=set)

    def check(self) -> None:
        if self.cancellation.is_set() or self.remaining() <= 0:
            raise ExecutionCancelled("case execution was cancelled")

    def remaining(self) -> float:
        if self.seconds is None:
            return float("inf")
        return max(0.0, self.seconds - (time.monotonic() - self._started))

    @contextmanager
    def publication(self) -> Iterator[None]:
        # Cancellation and a write must have a single, unambiguous order.
        with self._lock:
            self.check()
            yield

    def register_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            if self.cancellation.is_set():
                process.kill()
                process.communicate()
                self.check()
            self._processes.add(process)

    def unregister_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        with self._lock:
            self.cancellation.set()
            processes = list(self._processes)
        # Reap clients before publishing a fallback: they also write answer files.
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


_execution: ContextVar[Optional[ExecutionControl]] = ContextVar("case_execution", default=None)


def current_execution() -> Optional[ExecutionControl]:
    return _execution.get()


def check_active() -> None:
    control = current_execution()
    if control is not None:
        control.check()


@contextmanager
def execution_context(control: ExecutionControl) -> Iterator[None]:
    token = _execution.set(control)
    try:
        control.check()
        yield
    finally:
        _execution.reset(token)


@contextmanager
def publication() -> Iterator[None]:
    control = current_execution()
    if control is None:
        yield
    else:
        with control.publication():
            yield
