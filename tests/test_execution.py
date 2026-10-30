from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agent import solve_with_deadline
from core.execution import ExecutionCancelled, ExecutionControl, execution_context
from core.io import write_json, write_report
from tools import write_text_file


@pytest.mark.asyncio
async def test_timed_out_worker_cannot_overwrite_fallback(tmp_path):
    release = threading.Event()
    finished = threading.Event()
    rejected = threading.Event()
    path = tmp_path / "D-01.json"

    class SlowAgent:
        async def solve(self, question):
            def work():
                release.wait(2)
                try:
                    write_json(path, {"status": "completed"})
                except ExecutionCancelled:
                    rejected.set()
                    raise
                finally:
                    finished.set()
            return await asyncio.to_thread(work)

    try:
        with pytest.raises(asyncio.TimeoutError):
            await solve_with_deadline(SlowAgent(), {}, 0.03)
        write_json(path, {"status": "failed"})
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert rejected.is_set()
        assert json.loads(path.read_text()) == {"status": "failed"}
    finally:
        release.set()


def test_cancel_revokes_decorated_and_fallback_writes(tmp_path):
    control = ExecutionControl()
    control.cancel()
    with pytest.raises(ExecutionCancelled):
        with execution_context(control):
            write_text_file(str(tmp_path / "late.txt"), "late")
    assert not list(tmp_path.iterdir())


def test_cancel_terminates_registered_client_before_fallback(tmp_path):
    path = tmp_path / "D-01.json"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    control = ExecutionControl()
    try:
        control.register_process(process)
        control.cancel()
        assert process.poll() is not None
        write_json(path, {"status": "failed"})
        assert json.loads(path.read_text()) == {"status": "failed"}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_atomic_write_preserves_old_file_if_replace_fails(tmp_path, monkeypatch):
    from core.files import atomic_write_text
    path = tmp_path / "answer.json"
    path.write_text("old", encoding="utf-8")

    def denied(*args):
        raise PermissionError("replace denied")

    monkeypatch.setattr("core.files.os.replace", denied)
    with pytest.raises(PermissionError):
        atomic_write_text(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["answer.json"]


def test_report_save_checks_limits_and_preserves_exact_bytes(settings):
    markdown = "# 综述\n" + "证据" * 450 + "[1]\n## 参考文献\n[1] paper.pdf\n"
    relative = write_report(settings, "Survey", "C-01", markdown)
    assert (settings.workspace_root / relative).read_bytes() == markdown.encode("utf-8")
    from core.io import OutputError
    with pytest.raises(OutputError):
        write_report(settings, "Survey", "C-01", markdown.replace("证据" * 450, "证据" * 3000))
    assert (settings.workspace_root / relative).read_bytes() == markdown.encode("utf-8")


def test_model_request_is_capped_by_case_time_remaining(monkeypatch):
    import core.execution as execution
    import core.llm as llm
    from types import SimpleNamespace

    clock, seen = [0.0], []
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock[0])

    class Client:
        def __init__(self, **kwargs):
            seen.append(kwargs["timeout"])
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return SimpleNamespace(model_dump=lambda: {"choices": []})

    monkeypatch.setattr(llm, "_OpenAI", Client)
    control = ExecutionControl(seconds=540, _started=0)
    with execution_context(control):
        clock[0] = 500
        llm.ModelClient("http://unused.test/v1", "fake", "fake", timeout=300).complete_raw([])
    assert seen == [40]


def test_cancel_waits_for_client_start_and_registration(settings, tmp_path, monkeypatch):
    from d_adapter.runner import run_platform

    settings.d_client.write_text("# fake client, not executed", encoding="utf-8")
    starting, release_start, requested = threading.Event(), threading.Event(), threading.Event()
    killed, cancelled = threading.Event(), threading.Event()
    control = ExecutionControl()
    answer = tmp_path / "D-01.json"
    errors = []

    class ClientProcess:
        returncode = None

        def __init__(self, *args, **kwargs):
            starting.set()
            assert release_start.wait(2)
            # A client can publish as soon as Popen starts it. Cancellation
            # must wait for registration and reap it before writing fallback.
            write_json(answer, {"status": "completed"})

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1
            killed.set()

        def wait(self):
            assert killed.wait(2)
            return self.returncode

        def communicate(self, timeout=None):
            assert killed.wait(2)
            return '{"case_id":"D-01","status":"completed"}', ""

    monkeypatch.setattr("d_adapter.runner.subprocess.Popen", ClientProcess)

    def run():
        try:
            with execution_context(control):
                run_platform("D-01", Path("implementation.py"), settings, timeout=1)
        except Exception as exc:
            errors.append(exc)

    def cancel():
        requested.set()
        control.cancel()
        write_json(answer, {"status": "failed"})
        cancelled.set()

    worker = threading.Thread(target=run)
    canceller = threading.Thread(target=cancel)
    worker.start()
    try:
        assert starting.wait(2)
        canceller.start()
        assert requested.wait(2)
        assert not cancelled.wait(0.03)
    finally:
        release_start.set()
        worker.join(3)
        if canceller.ident is not None:
            canceller.join(3)
    assert not worker.is_alive() and not canceller.is_alive()
    assert cancelled.is_set() and killed.is_set()
    assert any(isinstance(exc, ExecutionCancelled) for exc in errors)
    assert json.loads(answer.read_text()) == {"status": "failed"}
