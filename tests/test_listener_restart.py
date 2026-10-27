"""On Windows one failed accept closes the page's listening socket while the process lives on: restart instead."""

import ast
import asyncio
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


class FakeLoop:
    def __init__(self):
        self.logged = []

    def default_exception_handler(self, context):
        self.logged.append(context["message"])


@pytest.fixture
def spawned(monkeypatch):
    out = []
    monkeypatch.setattr(app, "spawn", lambda coro, what: (out.append(what), coro.close()))
    monkeypatch.setitem(app.state, "restart_scheduled", False)
    return out


def test_a_failed_accept_schedules_one_idle_restart(spawned):
    loop = FakeLoop()
    app.on_loop_error(loop, {"message": "Accept failed on a socket", "exception": OSError(22, "x", None, 64)})
    app.on_loop_error(loop, {"message": "Accept failed on a socket", "exception": OSError(22, "x", None, 64)})
    assert spawned == ["restart after a failed accept"]
    assert loop.logged == ["Accept failed on a socket"] * 2  # the traceback still reaches the log


def test_other_loop_errors_are_only_logged(spawned):
    loop = FakeLoop()
    app.on_loop_error(loop, {"message": "Task exception was never retrieved"})
    assert spawned == [] and loop.logged == ["Task exception was never retrieved"]


def test_a_restart_already_scheduled_is_not_doubled(spawned):
    app.state["restart_scheduled"] = True
    app.on_loop_error(FakeLoop(), {"message": "Accept failed on a socket"})
    assert spawned == []


def test_the_service_installs_the_handler_at_start():
    first = ast.parse(textwrap.dedent(inspect.getsource(app.lifespan))).body[0].body[0]
    assert ast.unparse(first) == "asyncio.get_running_loop().set_exception_handler(on_loop_error)"


@pytest.mark.skipif(sys.platform != "win32", reason="only the Windows proactor closes the listener")
def test_the_real_proactor_failure_reaches_the_restart(spawned, caplog):
    """Pins what on_loop_error relies on: CPython's message for the failure, and that it closes the listener."""
    async def main():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(app.on_loop_error)

        def accept(sock):
            f = loop.create_future()
            f.set_exception(OSError(22, "The specified network name is no longer available", None, 64))
            return f
        loop._proactor.accept = accept
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            return [s.fileno() for s in server.sockets]
        finally:
            server.close()

    assert asyncio.run(main()) == [-1]
    assert spawned == ["restart after a failed accept"]
    assert "WinError 64" in caplog.text
