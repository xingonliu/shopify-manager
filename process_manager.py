import ctypes
from ctypes import wintypes
import subprocess
import sys
import threading
from pathlib import Path


# -- Types
class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("Basic", BasicLimits), ("IoCounters", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    def __init__(self):
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; handle is not inheritable.
        if not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        handle = kernel32.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self):
        if not kernel32.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            if not kernel32.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


class ProcessManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False

    def start(self, command, cwd, *, merge_stderr=False):
        with self.lock:
            if self.closed:
                raise RuntimeError("服务正在关闭，无法启动新任务")
            job = WindowsJob()
            proc = None
            try:
                # The launcher cannot spawn descendants until assignment succeeds.
                # If the server dies before assignment, stdin EOF makes it exit.
                proc = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), *command],
                    cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                job.assign(proc.pid)
                self.jobs[proc] = job
                proc.stdin.write("start\n")
                proc.stdin.close()
                threading.Thread(target=self.reap, args=(proc,), daemon=True).start()
                return proc
            except BaseException:
                self.jobs.pop(proc, None)
                job.close()
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
                raise

    def terminate(self, proc):
        with self.lock:
            job = self.jobs.get(proc)
            if job is not None:
                job.terminate()
        proc.wait(timeout=5)

    def reap(self, proc):
        proc.wait()
        self.release(proc)

    def release(self, proc):
        with self.lock:
            job = self.jobs.pop(proc, None)
            if job is not None:
                job.close()
        proc.wait(timeout=5)

    def shutdown(self):
        with self.lock:
            self.closed = True
            processes = list(self.jobs)
            for job in self.jobs.values():
                job.close()
            self.jobs.clear()
        for proc in processes:
            proc.wait(timeout=5)


# -- Constants
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE


# -- Functions
def launch_command():
    if sys.stdin.readline() != "start\n":
        return 1
    return subprocess.call(
        sys.argv[1:], stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


# -- Lifecycle
if __name__ == "__main__":
    sys.exit(launch_command())
