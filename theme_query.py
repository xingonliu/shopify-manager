import codecs
import threading
import time


# -- Types
class QueryError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class QueryOutput:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.chunks = {"stdout": [], "stderr": []}
        self.lock = threading.Lock()
        self.error = None

    def read(self, stream, name):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while data := stream.buffer.read1(4096):
                with self.lock:
                    if self.size + len(data) > self.limit:
                        self.error = "查询输出超过容量限制，已停止命令。"
                        return
                    self.size += len(data)
                    self.chunks[name].append(decoder.decode(data))
            with self.lock:
                self.chunks[name].append(decoder.decode(b"", final=True))
        except Exception as exc:
            with self.lock:
                self.error = f"读取查询输出失败: {exc}"
        finally:
            stream.close()

    def snapshot(self):
        with self.lock:
            return "".join(self.chunks["stdout"]), "".join(self.chunks["stderr"]), self.error


# -- Constants
QUERY_TIMEOUT = 45.0
OUTPUT_LIMIT = 1024 * 1024


# -- Functions
def run_theme_query(manager, command, cwd, cancelled, *, timeout=QUERY_TIMEOUT, output_limit=OUTPUT_LIMIT):
    proc = manager.start(command, cwd)
    output = QueryOutput(output_limit)
    readers = []
    deadline = time.monotonic() + timeout
    auth_deadline = None
    try:
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            reader = threading.Thread(target=output.read, args=(stream, name), daemon=True)
            reader.start()
            readers.append(reader)
        while True:
            finished = proc.poll() is not None and all(not reader.is_alive() for reader in readers)
            stdout, stderr, error = output.snapshot()
            combined = stderr + stdout
            now = time.monotonic()
            if cancelled.is_set():
                raise QueryError(499, "查询已取消。")
            if error:
                raise QueryError(502, error)
            if "To run this command, log in to Shopify" in combined and auth_deadline is None:
                auth_deadline = now + 1.8
            if "activate-with-code" in combined or (auth_deadline is not None and now >= auth_deadline):
                raise QueryError(401, f"获取模板列表失败: {combined.strip()}")
            if finished:
                if auth_deadline is not None:
                    raise QueryError(401, f"获取模板列表失败: {combined.strip()}")
                if proc.returncode:
                    raise QueryError(502, f"获取模板列表失败: {combined.strip() or '未知错误'}")
                return stdout
            if now >= deadline:
                raise QueryError(504, "查询远程模板超时，已停止命令，请稍后重试。")
            cancelled.wait(0.05)
    finally:
        # Closing the job kills descendants even if the direct command already exited.
        manager.release(proc)
        for reader in readers:
            reader.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if not stream.closed:
                stream.close()
