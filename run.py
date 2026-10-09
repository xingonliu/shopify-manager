import sys
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

py_dir = str(Path(sys.executable).resolve().parent)
dll_dir = os.path.join(py_dir, "DLLs")
for d in (py_dir, dll_dir):
    if os.path.exists(d):
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except Exception:
                pass

if sys.stdout is None:
    sys.stdout = open(BASE_DIR / "server.log", "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(BASE_DIR / "server.log", "a", encoding="utf-8", buffering=1)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=9290, log_level="info")
