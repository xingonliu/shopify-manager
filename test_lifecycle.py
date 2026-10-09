import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from process_manager import ProcessManager, kernel32
from theme_query import QueryError, run_theme_query
import server


# -- Constants
ROOT = Path(__file__).resolve().parent


# -- Functions
def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Condition did not become true")


def process_alive(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def python_command(code):
    return [sys.executable, "-u", "-c", code]


# -- Tests
class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.manager = ProcessManager()
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.manager.shutdown()
        self.directory.cleanup()

    def query(self, code, **kwargs):
        return run_theme_query(self.manager, python_command(code), str(ROOT), threading.Event(), **kwargs)

    def test_command_and_descendant_have_no_console(self):
        probe = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"
        command = ["cmd.exe", "/c", *python_command(probe)]
        result = run_theme_query(self.manager, command, str(ROOT), threading.Event())
        self.assertEqual(result.strip(), "0")

    def test_success_and_nonzero_exit(self):
        self.assertEqual(json.loads(self.query("print('[{\"id\": 1}]')")), [{"id": 1}])
        with self.assertRaises(QueryError) as caught:
            self.query("import sys; print('failed', file=sys.stderr); sys.exit(3)")
        self.assertEqual(caught.exception.status_code, 502)
        self.assertFalse(self.manager.jobs)

    def test_timeout_kills_grandchildren(self):
        pid_file = Path(self.directory.name) / "child.pid"
        code = f"import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
        with self.assertRaises(QueryError) as caught:
            self.query(code, timeout=0.8)
        self.assertEqual(caught.exception.status_code, 504)
        pid = int(pid_file.read_text())
        wait_for(lambda: not process_alive(pid))
        self.assertFalse(self.manager.jobs)

    def test_parent_command_exit_kills_remaining_child(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True)"
        pid = int(self.query(code, timeout=2).strip())
        wait_for(lambda: not process_alive(pid))

    def test_cancel_query(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.3, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(QueryError) as caught:
                run_theme_query(self.manager, python_command("import time; time.sleep(60)"), str(ROOT), cancelled)
            self.assertEqual(caught.exception.status_code, 499)
            self.assertFalse(self.manager.jobs)
        finally:
            timer.join()

    def test_auth_prompt_without_newline(self):
        with self.assertRaises(QueryError) as caught:
            self.query("import sys,time; sys.stderr.write('https://accounts.shopify.com/activate-with-code?test'); sys.stderr.flush(); time.sleep(60)", timeout=2)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(self.manager.jobs)

    def test_output_limit(self):
        with self.assertRaises(QueryError) as caught:
            self.query("import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(60)", output_limit=8192)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("容量限制", caught.exception.detail)
        self.assertFalse(self.manager.jobs)

    def test_server_force_exit_kills_tree(self):
        pid_file = Path(self.directory.name) / "descendant.pid"
        child = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
        owner_code = f"from process_manager import ProcessManager; import time; manager=ProcessManager(); manager.start({python_command(child)!r}, {str(ROOT)!r}); time.sleep(60)"
        owner = subprocess.Popen(python_command(owner_code), cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for(pid_file.exists)
            pid = int(pid_file.read_text())
            self.assertTrue(process_alive(pid))
            owner.kill()
            owner.wait(timeout=5)
            wait_for(lambda: not process_alive(pid))
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)

    def test_shutdown_kills_children_and_rejects_start(self):
        proc = self.manager.start(python_command("import os,time; print(os.getpid(),flush=True); time.sleep(60)"), str(ROOT))
        pid = int(proc.stdout.readline())
        self.manager.shutdown()
        wait_for(lambda: not process_alive(pid))
        with self.assertRaises(RuntimeError):
            self.manager.start(python_command("print(1)"), str(ROOT))
        proc.stdout.close()
        proc.stderr.close()


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        server.process_manager = ProcessManager()
        server.tasks.clear()

    def tearDown(self):
        server.process_manager.shutdown()

    def test_concurrent_restart_and_stop(self):
        state = server.get_task_state("test")
        commands = python_command("import time; print('ready',flush=True); time.sleep(60)")
        threads = [threading.Thread(target=server.run_command_in_background, args=("test", commands, str(ROOT), "command")) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        current = state["process"]
        self.assertIsNotNone(current)
        time.sleep(0.2)
        self.assertIs(state["process"], current)
        self.assertIsNone(current.poll())
        self.assertEqual(state["status"], "running")
        wait_for(lambda: len(server.process_manager.jobs) == 1)
        server.stop_process("test")
        wait_for(lambda: not server.process_manager.jobs)
        self.assertIsNone(state["process"])
        self.assertEqual(state["status"], "idle")

    def test_log_limits(self):
        state = server.get_task_state("test")
        queue = asyncio.Queue(maxsize=2)
        state["listeners"].add(queue)
        for number in range(4):
            server.deliver_log(state, str(number))
        self.assertEqual([queue.get_nowait(), queue.get_nowait()], ["2", "3"])
        server.broadcast_log("test", "x" * 100000)
        self.assertEqual(len(state["logs"][-1]), 4096)

    def test_log_delivery_scheduling_is_bounded(self):
        from unittest.mock import Mock
        fake_loop = Mock()
        fake_loop.is_running.return_value = True
        with patch.object(server, "loop", fake_loop):
            for _ in range(3000):
                server.broadcast_log("test", "line")
        state = server.get_task_state("test")
        self.assertEqual(len(state["logs"]), 2000)
        self.assertEqual(len(state["pending_logs"]), 256)
        self.assertEqual(fake_loop.call_soon_threadsafe.call_count, 1)

    def test_lifespan_shutdown_and_idle_websocket_disconnect(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/logs/socket-test"):
                wait_for(lambda: len(server.get_task_state("socket-test")["listeners"]) == 1)
            wait_for(lambda: not server.get_task_state("socket-test")["listeners"])
            server.run_command_in_background("test", python_command("import time; time.sleep(60)"), str(ROOT), "command")
            proc = server.get_task_state("test")["process"]
        self.assertIsNotNone(proc.poll())
        self.assertFalse(server.process_manager.jobs)
        self.assertTrue(server.process_manager.closed)


class QueryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.tasks.clear()
        server.process_manager = ProcessManager()

    async def asyncTearDown(self):
        server.process_manager.shutdown()

    async def test_duplicate_and_disconnect(self):
        class Request:
            disconnected = False

            async def is_disconnected(self):
                return self.disconnected

        started = threading.Event()
        ended = threading.Event()

        def query(manager, command, cwd, cancelled):
            started.set()
            cancelled.wait(5)
            ended.set()
            raise QueryError(499, "cancelled")

        request = Request()
        with patch.object(server, "load_stores", return_value=[{"id": "test", "domain": "test", "directory": str(ROOT)}]), patch.object(server, "run_theme_query", side_effect=query):
            first = asyncio.create_task(server.get_store_themes("test", request))
            await asyncio.to_thread(started.wait, 2)
            with self.assertRaises(server.HTTPException) as duplicate:
                await server.get_store_themes("test", Request())
            self.assertEqual(duplicate.exception.status_code, 409)
            request.disconnected = True
            with self.assertRaises(server.HTTPException) as cancelled:
                await first
            self.assertEqual(cancelled.exception.status_code, 499)
            self.assertTrue(ended.is_set())
            self.assertFalse(server.get_task_state("test")["query_lock"].locked())

    async def test_request_task_cancel_waits_for_cleanup(self):
        class Request:
            async def is_disconnected(self):
                return False

        started = threading.Event()
        ended = threading.Event()

        def query(manager, command, cwd, cancelled):
            started.set()
            cancelled.wait(5)
            ended.set()
            raise QueryError(499, "cancelled")

        with patch.object(server, "load_stores", return_value=[{"id": "test", "domain": "test", "directory": str(ROOT)}]), patch.object(server, "run_theme_query", side_effect=query):
            work = asyncio.create_task(server.get_store_themes("test", Request()))
            await asyncio.to_thread(started.wait, 2)
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
            self.assertTrue(ended.is_set())
            self.assertFalse(server.get_task_state("test")["query_lock"].locked())


# -- Lifecycle
if __name__ == "__main__":
    unittest.main()
