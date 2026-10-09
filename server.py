import os
import sys
import json
import uuid
import re
import time
import asyncio
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from collections import deque
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from process_manager import ProcessManager
from theme_query import QueryError, run_theme_query

# -- Types
class FolderSelectPayload(BaseModel):
    initial_dir: Optional[str] = None


class DirectoryUpdatePayload(BaseModel):
    directory: str


class ActionPayload(BaseModel):
    theme_id: Optional[Any] = None
    theme_role: Optional[str] = None
    allow_live: Optional[bool] = False
    custom_name: Optional[str] = None
    store_password: Optional[str] = None
    theme_password: Optional[str] = None


class StoreModel(BaseModel):
    name: str
    domain: str
    directory: str
    store_password: Optional[str] = ""
    theme_password: Optional[str] = ""
    pinned: Optional[bool] = False
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    used_at: Optional[int] = None


class CommandModel(BaseModel):
    name: str
    directory: str
    command: str
    description: Optional[str] = ""


class PromptRespondPayload(BaseModel):
    prompt_type: str
    value: str
    save: bool = True


# -- Constants
BASE_DIR = Path(__file__).resolve().parent
STORES_FILE = BASE_DIR / "stores.json"
COMMANDS_FILE = BASE_DIR / "commands.json"

# -- State
if sys.stdout is None:
    sys.stdout = open(BASE_DIR / "server.log", "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = open(BASE_DIR / "server.log", "a", encoding="utf-8", buffering=1)


tasks_lock = threading.RLock()
process_manager = ProcessManager()
tasks: Dict[str, Dict[str, Any]] = {}
loop: Optional[asyncio.AbstractEventLoop] = None


app = FastAPI(title="Shopify Theme Local Manager", lifespan=lambda app: lifespan(app))


# -- Functions
def get_task_state(store_id: str) -> Dict[str, Any]:
    with tasks_lock:
        if store_id not in tasks:
            tasks[store_id] = new_task_state()
        return tasks[store_id]


def new_task_state():
    return {
        "process": None, "type": "idle", "status": "idle",
        "logs": deque(maxlen=2000), "listeners": set(),
        "prompt_needed": None, "last_action": None,
        "lock": threading.RLock(), "query_lock": threading.Lock(),
        "query_cancel": None,
        "pending_logs": deque(maxlen=256), "log_delivery_pending": False,
    }



def load_stores():
    if not STORES_FILE.exists():
        return []
    try:
        with open(STORES_FILE, "r", encoding="utf-8") as f:
            stores = json.load(f)
            now_ts = int(time.time())
            modified = False
            for idx, s in enumerate(stores):
                # 为旧版本中未写入时间戳的历史商店补充基准时间戳，保证时间梯度
                fallback_time = now_ts - (len(stores) - idx) * 3600
                if "created_at" not in s or s["created_at"] is None:
                    s["created_at"] = fallback_time
                    modified = True
                if "updated_at" not in s or s["updated_at"] is None:
                    s["updated_at"] = s["created_at"]
                    modified = True
                if "used_at" not in s or s["used_at"] is None:
                    s["used_at"] = s["created_at"]
                    modified = True
            if modified:
                save_stores(stores)
            return stores
    except Exception:
        return []


def save_stores(stores):
    with open(STORES_FILE, "w", encoding="utf-8") as f:
        json.dump(stores, f, ensure_ascii=False, indent=2)


def load_commands():
    if not COMMANDS_FILE.exists():
        return []
    try:
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_commands(commands):
    with open(COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(commands, f, ensure_ascii=False, indent=2)


def deliver_log(state, line):
    for queue in list(state["listeners"]):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(line)


def flush_logs(state):
    with state["lock"]:
        pending = list(state["pending_logs"])
        state["pending_logs"].clear()
        state["log_delivery_pending"] = False
    for line in pending:
        deliver_log(state, line)


def broadcast_log(store_id: str, line: str):
    state = get_task_state(store_id)
    line = line[:4096]
    with state["lock"]:
        state["logs"].append(line)
        if loop and loop.is_running():
            state["pending_logs"].append(line)
            if not state["log_delivery_pending"]:
                state["log_delivery_pending"] = True
                loop.call_soon_threadsafe(flush_logs, state)


def stop_process(store_id: str):
    state = get_task_state(store_id)
    with state["lock"]:
        if state["query_cancel"] is not None:
            state["query_cancel"].set()
        proc = state["process"]
        if proc is not None:
            process_manager.terminate(proc)
        state["process"] = None
        state["status"] = "idle"
        state["type"] = "idle"
        state["prompt_needed"] = None
        broadcast_log(store_id, "\n[系统] 进程已被手动停止。\n")


def run_command_in_background(store_id: str, cmd: list, cwd: str, task_type: str, action_info: Optional[dict] = None):
    state = get_task_state(store_id)
    with state["lock"]:
        if state["process"] is not None:
            stop_process(store_id)
        proc = process_manager.start(cmd, cwd, merge_stderr=True)
        state["process"] = proc
        state["type"] = task_type
        state["status"] = "running"
        state["prompt_needed"] = None
        state["last_action"] = action_info
        broadcast_log(store_id, f"\n=== 正在启动任务 [{task_type}] ===\n工作目录: {cwd}\n")
        worker = threading.Thread(target=read_task_output, args=(store_id, state, proc, task_type), daemon=True)
        try:
            worker.start()
        except BaseException:
            process_manager.release(proc)
            proc.stdout.close()
            state["process"] = None
            state["status"] = "error"
            state["type"] = "idle"
            raise


def read_task_output(store_id, state, proc, task_type):
    try:
        for line in iter(lambda: proc.stdout.readline(4096), ""):
            with state["lock"]:
                if state["process"] is not proc:
                    continue
                broadcast_log(store_id, line)
                lower_line = line.lower()
                if "enter your store password" in lower_line or ("failed to prompt" in lower_line and "store password" in lower_line):
                    state["prompt_needed"] = {
                        "type": "store_password", "title": "需要前台访问密码 (Storefront Password)",
                        "message": "目标商店启用了前台密码保护，请在下方输入密码后重试：",
                    }
                elif "password generated from the theme access app" in lower_line or "theme access" in lower_line:
                    state["prompt_needed"] = {
                        "type": "theme_password", "title": "需要 Theme Access 访问令牌",
                        "message": "该操作需要 Theme Access App 生成的密码或 Admin API 令牌，请输入：",
                    }
        return_code = proc.wait()
        with state["lock"]:
            if state["process"] is proc:
                state["status"] = "completed" if return_code == 0 else "error"
                broadcast_log(store_id, f"\n=== 任务 [{task_type}] 结束 (退出码: {return_code}) ===\n")
    except Exception as exc:
        with state["lock"]:
            if state["process"] is proc:
                state["status"] = "error"
                broadcast_log(store_id, f"\n[执行异常] {exc}\n")
    finally:
        process_manager.release(proc)
        proc.stdout.close()
        with state["lock"]:
            # An old reader must never clear the new task's process or status.
            if state["process"] is proc:
                state["type"] = "idle"
                state["process"] = None


# ========================== API 路由 ==========================

@app.get("/api/stores")
def get_stores():
    stores = load_stores()
    for s in stores:
        sid = s["id"]
        st = get_task_state(sid)
        proc = st["process"]
        is_running = proc is not None and proc.poll() is None
        s["running"] = is_running
        s["task_type"] = st["type"] if is_running else "idle"
        s["status"] = st["status"] if is_running else ("idle" if st["status"] == "running" else st["status"])
        s["prompt_needed"] = st.get("prompt_needed")
    return stores


@app.post("/api/stores")
def create_store(store: StoreModel):
    stores = load_stores()
    clean_domain = store.domain.strip()
    clean_domain = re.sub(r"^https?://", "", clean_domain)
    clean_domain = re.sub(r"/.*$", "", clean_domain)

    clean_dir = store.directory.strip().rstrip("\\/")
    if clean_dir and not Path(clean_dir).exists():
        raise HTTPException(status_code=400, detail=f"指定的本地文件夹不存在: {clean_dir}")

    new_id = re.sub(r"[^a-zA-Z0-9_-]", "", store.name.lower()) or str(uuid.uuid4())[:8]
    existing_ids = {s["id"] for s in stores}
    counter = 1
    final_id = new_id
    while final_id in existing_ids:
        final_id = f"{new_id}_{counter}"
        counter += 1

    now_ts = int(time.time())
    item = {
        "id": final_id,
        "name": store.name.strip(),
        "domain": clean_domain,
        "directory": clean_dir,
        "store_password": (store.store_password or "").strip(),
        "theme_password": (store.theme_password or "").strip(),
        "pinned": bool(store.pinned),
        "created_at": now_ts,
        "updated_at": now_ts,
        "used_at": now_ts,
    }
    stores.append(item)
    save_stores(stores)
    return item


@app.put("/api/stores/{store_id}")
def update_store(store_id: str, store: StoreModel):
    stores = load_stores()
    target = None
    for s in stores:
        if s["id"] == store_id:
            target = s
            break
    if not target:
        raise HTTPException(status_code=404, detail="未找到该商店")

    clean_domain = store.domain.strip()
    clean_domain = re.sub(r"^https?://", "", clean_domain)
    clean_domain = re.sub(r"/.*$", "", clean_domain)

    clean_dir = store.directory.strip().rstrip("\\/")
    if clean_dir and not Path(clean_dir).exists():
        raise HTTPException(status_code=400, detail=f"指定的本地文件夹不存在: {clean_dir}")

    target["name"] = store.name.strip()
    target["domain"] = clean_domain
    target["directory"] = clean_dir
    target["store_password"] = (store.store_password or "").strip()
    target["theme_password"] = (store.theme_password or "").strip()
    target["updated_at"] = int(time.time())
    if store.pinned is not None:
        target["pinned"] = bool(store.pinned)
    save_stores(stores)
    return target


@app.post("/api/stores/{store_id}/touch")
def touch_store(store_id: str):
    stores = load_stores()
    target = next((s for s in stores if s["id"] == store_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该商店")
    now_ts = int(time.time())
    target["used_at"] = now_ts
    save_stores(stores)
    return {"success": True, "used_at": now_ts}


@app.post("/api/stores/{store_id}/pin")
def toggle_pin_store(store_id: str):
    stores = load_stores()
    target = next((s for s in stores if s["id"] == store_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该商店")
    target["pinned"] = not target.get("pinned", False)
    save_stores(stores)
    return {"success": True, "pinned": target["pinned"]}


@app.delete("/api/stores/{store_id}")
def delete_store(store_id: str):
    stores = load_stores()
    target = next((s for s in stores if s["id"] == store_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该商店")
    stop_process(store_id)
    stores = [s for s in stores if s["id"] != store_id]
    save_stores(stores)
    return {"success": True}




def select_folder_native(initial_dir: Optional[str] = None) -> str:
    init_path = ""
    if initial_dir and Path(initial_dir).exists():
        init_path = str(Path(initial_dir).resolve())

    # 方式一：Tkinter 弹窗选择
    try:
        py_code = (
            "import sys, os, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.wm_attributes('-topmost', 1)\n"
            "init = sys.argv[1] if len(sys.argv) > 1 and os.path.exists(sys.argv[1]) else None\n"
            "folder = filedialog.askdirectory(title='选择 Shopify 本地主题根目录', initialdir=init)\n"
            "if folder:\n"
            "    print(os.path.normpath(folder))\n"
        )
        cmd = [sys.executable, "-c", py_code]
        if init_path:
            cmd.append(init_path)
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = res.stdout.strip()
        if out:
            return out
        if res.returncode == 0:
            return ""
    except Exception:
        pass

    # 方式二：PowerShell 弹窗选择备用
    try:
        ps_code = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = '请选择 Shopify 本地主题根目录'; "
            "$f.ShowNewFolderButton = $true; "
            "$form = New-Object System.Windows.Forms.Form; "
            "$form.TopMost = $true; "
            "if ($f.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }"
        )
        res = subprocess.run(
            ["powershell.exe", "-sta", "-NoProfile", "-Command", ps_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return res.stdout.strip()
    except Exception:
        return ""


@app.post("/api/select-folder")
async def api_select_folder(payload: Optional[FolderSelectPayload] = None):
    init = payload.initial_dir if payload else None
    folder = await asyncio.to_thread(select_folder_native, init)
    return {"path": folder}




@app.post("/api/stores/{store_id}/directory")
def set_store_directory(store_id: str, payload: DirectoryUpdatePayload):
    stores = load_stores()
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="未找到该商店")
    clean_dir = payload.directory.strip().strip('"\'').rstrip("\\/")
    if clean_dir and not Path(clean_dir).exists():
        raise HTTPException(status_code=400, detail=f"指定的本地目录不存在: {clean_dir}")
    store["directory"] = clean_dir
    store["updated_at"] = int(time.time())
    save_stores(stores)
    return {"success": True, "path": clean_dir, "store": store}


@app.post("/api/stores/{store_id}/bind-folder")
async def api_bind_folder(store_id: str):
    stores = load_stores()
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="未找到该商店")
    init = store.get("directory")
    folder = await asyncio.to_thread(select_folder_native, init)
    if not folder:
        return {"success": False, "cancelled": True}
    if not Path(folder).exists():
        raise HTTPException(status_code=400, detail="所选文件夹不存在")
    store["directory"] = folder
    store["updated_at"] = int(time.time())
    save_stores(stores)
    return {"success": True, "path": folder, "store": store}


@app.get("/api/stores/{store_id}/themes")
async def get_store_themes(store_id: str, request: Request):
    store = next((store for store in load_stores() if store["id"] == store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="未找到该商店")
    state = get_task_state(store_id)
    if not state["query_lock"].acquire(blocking=False):
        raise HTTPException(status_code=409, detail="该商店已有模板查询正在进行。")
    cancelled = threading.Event()
    work = None
    try:
        with state["lock"]:
            state["query_cancel"] = cancelled
        directory = store.get("directory")
        if not directory or not Path(directory).exists():
            directory = str(BASE_DIR)
        command = ["cmd.exe", "/c", "shopify", "theme", "list", "--store", store["domain"], "--json"]
        if store.get("theme_password"):
            command.extend(["--password", store["theme_password"]])
        work = asyncio.create_task(asyncio.to_thread(run_theme_query, process_manager, command, directory, cancelled))
        while not work.done():
            if await request.is_disconnected():
                cancelled.set()
            await asyncio.wait({work}, timeout=0.1)
        stdout = work.result()
        try:
            match = re.search(r"\[\s*\{.*\}\s*\]", stdout, re.DOTALL)
            return json.loads(match.group(0) if match else stdout)
        except (ValueError, TypeError):
            raise HTTPException(status_code=502, detail="解析模板列表数据失败，返回格式不符合预期。")
    except QueryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        cancelled.set()
        try:
            if work is not None:
                try:
                    await asyncio.shield(work)
                except QueryError:
                    pass
        finally:
            with state["lock"]:
                state["query_cancel"] = None
            state["query_lock"].release()


@app.post("/api/stores/{store_id}/action/{action}")
def trigger_action(store_id: str, action: str, payload: Optional[ActionPayload] = None):
    stores = load_stores()
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="未找到该商店")

    d = store.get("directory", "")
    if not d or not Path(d).exists():
        raise HTTPException(status_code=400, detail="该商店尚未绑定有效的本地主题目录，请先编辑绑定。")

    store["used_at"] = int(time.time())
    save_stores(stores)

    domain = store["domain"]
    store_pwd = (payload.store_password if payload and payload.store_password else store.get("store_password", "")).strip()
    theme_pwd = (payload.theme_password if payload and payload.theme_password else store.get("theme_password", "")).strip()

    if action == "stop":
        stop_process(store_id)
        return {"success": True, "message": "已停止"}

    if action == "dev":
        cmd = ["cmd.exe", "/c", "shopify", "theme", "dev", "--store", domain]
        if store_pwd:
            cmd.extend(["--store-password", store_pwd])
        if theme_pwd:
            cmd.extend(["--password", theme_pwd])
        run_command_in_background(store_id, cmd, d, "dev", {"action": "dev", "payload": payload.dict() if payload else {}})
        return {"success": True, "message": "已启动开发监听"}

    if action == "pull":
        cmd = ["cmd.exe", "/c", "shopify", "theme", "pull", "--store", domain]
        is_live = bool(payload and (
            (payload.theme_role and payload.theme_role.lower() == "live") or
            (str(payload.theme_id).strip().lower() == "live")
        ))
        if is_live:
            cmd.append("--live")
        elif payload and payload.theme_id:
            cmd.extend(["-t", str(payload.theme_id)])
        else:
            cmd.append("-d")
        if theme_pwd:
            cmd.extend(["--password", theme_pwd])
        run_command_in_background(store_id, cmd, d, "pull", {"action": "pull", "payload": payload.dict() if payload else {}})
        return {"success": True, "message": "已触发拉取"}

    if action == "push":
        cmd = ["cmd.exe", "/c", "shopify", "theme", "push", "--store", domain]
        is_live = bool(payload and (
            (payload.theme_role and payload.theme_role.lower() == "live") or
            (str(payload.theme_id).strip().lower() == "live")
        ))
        if is_live:
            if not payload or not payload.allow_live:
                raise HTTPException(status_code=400, detail="推送到线上正式主题需要确认授权 (allow_live)")
            cmd.extend(["--live", "--allow-live"])
        elif payload and str(payload.theme_id).lower() == "new":
            cmd.append("--unpublished")
            if payload.custom_name:
                cmd.extend(["--theme", payload.custom_name])
        elif payload and payload.theme_id:
            cmd.extend(["-t", str(payload.theme_id)])
            if payload.allow_live:
                cmd.append("--allow-live")
        else:
            cmd.append("-d")
            if payload and payload.allow_live:
                cmd.append("--allow-live")
        if theme_pwd:
            cmd.extend(["--password", theme_pwd])
        run_command_in_background(store_id, cmd, d, "push", {"action": "push", "payload": payload.dict() if payload else {}})
        return {"success": True, "message": "已触发推送"}

    if action == "open-folder":
        try:
            resolved = str(Path(d).resolve())
            if sys.platform == "win32":
                os.startfile(resolved)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", resolved])
            else:
                subprocess.Popen(["xdg-open", resolved])
            return {"success": True, "message": "已在资源管理器中打开"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"打开文件夹失败: {e}")

    raise HTTPException(status_code=400, detail="不支持的操作")


@app.post("/api/stores/{store_id}/prompt-respond")
def respond_prompt(store_id: str, payload: PromptRespondPayload):
    stores = load_stores()
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="未找到该商店")

    val = payload.value.strip()
    if payload.save:
        if payload.prompt_type == "store_password":
            store["store_password"] = val
        elif payload.prompt_type == "theme_password":
            store["theme_password"] = val
        save_stores(stores)

    state = get_task_state(store_id)
    state["prompt_needed"] = None
    last_act = state.get("last_action")
    if not last_act:
        return {"success": True, "message": "密码已录入并保存"}

    act = last_act.get("action")
    raw_payload = last_act.get("payload") or {}
    act_payload = ActionPayload(**raw_payload)

    if payload.prompt_type == "store_password":
        act_payload.store_password = val
    elif payload.prompt_type == "theme_password":
        act_payload.theme_password = val

    broadcast_log(store_id, f"\n[系统] 已捕获输入凭证，正在重新执行任务 [{act}]...\n")
    return trigger_action(store_id, act, act_payload)


@app.get("/api/stores/{store_id}/logs")
def get_logs(store_id: str):
    state = get_task_state(store_id)
    return {"logs": list(state["logs"]), "prompt_needed": state.get("prompt_needed")}


@app.delete("/api/stores/{store_id}/logs")
def clear_logs(store_id: str):
    state = get_task_state(store_id)
    state["logs"].clear()
    state["prompt_needed"] = None
    return {"success": True}


# ========================== 快速命令 API 路由 ==========================

@app.get("/api/commands")
def get_commands():
    commands = load_commands()
    for c in commands:
        cid = c["id"]
        st = get_task_state(cid)
        proc = st["process"]
        is_running = proc is not None and proc.poll() is None
        c["running"] = is_running
        c["task_type"] = st["type"] if is_running else "idle"
        c["status"] = st["status"] if is_running else ("idle" if st["status"] == "running" else st["status"])
    return commands


@app.post("/api/commands")
def create_command(cmd_item: CommandModel):
    commands = load_commands()
    clean_dir = cmd_item.directory.strip().rstrip("\\/")
    if clean_dir and not Path(clean_dir).exists():
        raise HTTPException(status_code=400, detail=f"指定的本地文件夹不存在: {clean_dir}")

    clean_cmd = cmd_item.command.strip()
    if not clean_cmd:
        raise HTTPException(status_code=400, detail="执行命令不能为空")

    new_id = re.sub(r"[^a-zA-Z0-9_-]", "", cmd_item.name.lower()) or str(uuid.uuid4())[:8]
    existing_ids = {c["id"] for c in commands}
    counter = 1
    final_id = new_id
    while final_id in existing_ids:
        final_id = f"{new_id}_{counter}"
        counter += 1

    item = {
        "id": final_id,
        "name": cmd_item.name.strip(),
        "directory": clean_dir,
        "command": clean_cmd,
        "description": (cmd_item.description or "").strip(),
    }
    commands.append(item)
    save_commands(commands)
    return item


@app.put("/api/commands/{command_id}")
def update_command(command_id: str, cmd_item: CommandModel):
    commands = load_commands()
    target = next((c for c in commands if c["id"] == command_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该快速命令")

    clean_dir = cmd_item.directory.strip().rstrip("\\/")
    if clean_dir and not Path(clean_dir).exists():
        raise HTTPException(status_code=400, detail=f"指定的本地文件夹不存在: {clean_dir}")

    clean_cmd = cmd_item.command.strip()
    if not clean_cmd:
        raise HTTPException(status_code=400, detail="执行命令不能为空")

    target["name"] = cmd_item.name.strip()
    target["directory"] = clean_dir
    target["command"] = clean_cmd
    target["description"] = (cmd_item.description or "").strip()
    save_commands(commands)
    return target


@app.delete("/api/commands/{command_id}")
def delete_command(command_id: str):
    commands = load_commands()
    target = next((c for c in commands if c["id"] == command_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该快速命令")
    stop_process(command_id)
    commands = [c for c in commands if c["id"] != command_id]
    save_commands(commands)
    return {"success": True}


@app.post("/api/commands/{command_id}/action/{action}")
def trigger_command_action(command_id: str, action: str):
    commands = load_commands()
    cmd_item = next((c for c in commands if c["id"] == command_id), None)
    if not cmd_item:
        raise HTTPException(status_code=404, detail="未找到该快速命令")

    d = cmd_item.get("directory", "")
    if not d or not Path(d).exists():
        raise HTTPException(status_code=400, detail="该命令尚未绑定有效的本地目录，请先编辑绑定。")

    if action == "stop":
        stop_process(command_id)
        return {"success": True, "message": "已停止"}

    if action in ("start", "run"):
        command_str = cmd_item.get("command", "").strip()
        if not command_str:
            raise HTTPException(status_code=400, detail="执行命令为空，无法启动。")
        cmd = ["cmd.exe", "/c", command_str]
        run_command_in_background(command_id, cmd, d, "command", {"action": "command", "command": command_str})
        return {"success": True, "message": "已启动快速命令"}

    if action == "open-folder":
        try:
            resolved = str(Path(d).resolve())
            if sys.platform == "win32":
                os.startfile(resolved)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", resolved])
            else:
                subprocess.Popen(["xdg-open", resolved])
            return {"success": True, "message": "已在资源管理器中打开"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"打开文件夹失败: {e}")

    raise HTTPException(status_code=400, detail="不支持的操作")


@app.get("/api/commands/{command_id}/logs")
def get_command_logs(command_id: str):
    state = get_task_state(command_id)
    return {"logs": list(state["logs"])}


@app.delete("/api/commands/{command_id}/logs")
def clear_command_logs(command_id: str):
    state = get_task_state(command_id)
    state["logs"].clear()
    return {"success": True}


# ========================== WebSocket 实时日志 ==========================

@app.websocket("/ws/logs/{store_id}")
async def websocket_logs(websocket: WebSocket, store_id: str):
    await websocket.accept()
    state = get_task_state(store_id)

    for line in list(state["logs"]):
        await websocket.send_text(line)

    q = asyncio.Queue(maxsize=256)
    state["listeners"].add(q)
    try:
        async def send_logs():
            while True:
                await websocket.send_text(await q.get())

        async def receive_disconnect():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        sender = asyncio.create_task(send_logs())
        receiver = asyncio.create_task(receive_disconnect())
        try:
            done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        state["listeners"].discard(q)


# ========================== 单页仪表盘 UI ==========================

@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return FileResponse(BASE_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


# -- Lifecycle
@asynccontextmanager
async def lifespan(app):
    global loop, process_manager
    loop = asyncio.get_running_loop()
    process_manager = ProcessManager()
    try:
        yield
    finally:
        with tasks_lock:
            states = list(tasks.values())
        for state in states:
            with state["lock"]:
                if state["query_cancel"] is not None:
                    state["query_cancel"].set()
        await asyncio.to_thread(process_manager.shutdown)
        loop = None



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9290, log_level="info")
