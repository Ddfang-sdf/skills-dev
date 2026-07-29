#!/usr/bin/env python3
"""CLI 入口 — 读取 inbox/task.json，安全检查，调度执行，写 outbox/result.json"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ---- 路径 ----
# PyInstaller 打包后 __file__ 指向临时解压目录，用 sys.executable 获取实际 exe 位置
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent


def _resolve_skill_root(script_dir: Path) -> Path:
    """定位 skill 根目录（含 SKILL.md / inbox 的那一层）。

    兼容两种布局：脚本位于 <root>/scripts/ 下，或 exe 直接位于 <root>/ 下。
    """
    for cand in (script_dir, script_dir.parent):
        if (cand / "SKILL.md").exists() or (cand / "inbox").exists():
            return cand
    # 默认：目录名为 scripts 则取上级，否则取自身
    return script_dir.parent if script_dir.name == "scripts" else script_dir


SKILL_ROOT = _resolve_skill_root(SCRIPT_DIR)
INBOX_DIR = SKILL_ROOT / "inbox"
OUTBOX_DIR = SKILL_ROOT / "outbox"
INBOX_FILE = INBOX_DIR / "task.json"  # 单 agent 默认文件（向后兼容）
OUTBOX_FILE = OUTBOX_DIR / "result.json"
# config 优先搜 scripts/，exe 模式下 fallback 到 bin/——与 daemon 保持一致
def _resolve_config_file():
    for d in (SKILL_ROOT / "scripts", SCRIPT_DIR, SKILL_ROOT):
        cfg = d / "env_config.json"
        if cfg.exists():
            return cfg
    return SCRIPT_DIR / "env_config.json"
CONFIG_FILE = _resolve_config_file()
DAEMON_HOST = "127.0.0.1"
# 端口文件：与 env_config.json 同目录，记录本 skill 实例独占的 daemon 端口。
# 实现多 Agent 隔离（不同安装目录各自一个 daemon，互不干扰）。
PORT_FILE = CONFIG_FILE.parent / "daemon.port"
DAEMON_SCRIPT = SCRIPT_DIR / "ssh_daemon.py"
MAX_RETRIES = 3
RETRY_DELAY = 2

# 确保 scripts/ 在 sys.path 中（不同调用方式下路径可能不同）
import sys as _sys
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in _sys.path:
    _sys.path.insert(0, _script_dir)

from command_guard import CommandGuard, CheckResult


def _get_daemon_cmd():
    """获取启动 daemon 的命令。依次检查 bin/、scripts/、源码。"""
    # bin/ 优先（exe 发布包）
    daemon_exe = SKILL_ROOT / "bin" / "ssh-daemon.exe"
    if daemon_exe.exists():
        return [str(daemon_exe)]
    # scripts/ 兼容旧布局
    daemon_exe = SCRIPT_DIR / "ssh-daemon.exe"
    if daemon_exe.exists():
        return [str(daemon_exe)]
    return [sys.executable, str(DAEMON_SCRIPT)]


# ---- 工具函数 ----

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"environments": {}}


def _resolve_daemon_port() -> Optional[int]:
    """读取 daemon.port 文件，返回本实例的 daemon 端口。文件不存在返回 None。"""
    if PORT_FILE.exists():
        try:
            return int(PORT_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            return None
    return None


def _try_connect_daemon(port: int) -> Optional[socket.socket]:
    try:
        sock = socket.create_connection((DAEMON_HOST, port), timeout=2)
        return sock
    except Exception:
        return None


def _is_daemon_port_open(port: int) -> bool:
    try:
        sock = socket.create_connection((DAEMON_HOST, port), timeout=0.5)
        sock.close()
        return True
    except Exception:
        return False


def _normalize_daemon_resp(resp: dict) -> dict:
    """把 daemon 响应统一为结果字典；error 转为结构化失败结果。"""
    if "result" in resp:
        return resp["result"]
    err = resp.get("error") or {}
    return {"success": False,
            "error_code": err.get("code", "DAEMON_ERROR"),
            "stderr": err.get("message", str(resp))}


# ---- Executor ----

class Executor:
    def execute(self, target: str, command: str, timeout: int = 30,
                escalate: bool = False, as_credential: str = None) -> dict:
        raise NotImplementedError
    def upload(self, target: str, files, as_credential: str = None) -> dict:
        raise NotImplementedError
    def download(self, target: str, files, as_credential: str = None) -> dict:
        raise NotImplementedError
    def list_sessions(self) -> dict:
        raise NotImplementedError
    def disconnect(self, target: str) -> dict:
        raise NotImplementedError
    def close(self):
        pass


class DaemonExecutor(Executor):
    """通过 daemon TCP 连接执行。"""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._sock.settimeout(3600)  # 最长命令超时，避免 readline 无限阻塞
        self._file = sock.makefile("rw", encoding="utf-8")
        import threading
        self._lock = threading.Lock()
        self._req_id = 0

    def _call(self, method: str, params: dict) -> dict:
        with self._lock:
            self._req_id += 1
            req = {"id": f"req-{self._req_id}", "method": method, "params": params}
            self._file.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._file.flush()
            line = self._file.readline()
            if not line:
                raise ConnectionError("daemon 连接断开")
            return json.loads(line)

    def execute(self, target: str, command: str, timeout: int = 30,
                escalate: bool = False, as_credential: str = None) -> dict:
        params = {"target": target, "command": command, "timeout": timeout}
        if escalate:
            params["escalate"] = True
        if as_credential:
            params["as"] = as_credential
        try:
            result = _normalize_daemon_resp(self._call("execute", params))
        except (TimeoutError, ConnectionError, OSError) as e:
            print(f"[WARN] daemon 无响应，降级为直连: {e}", file=sys.stderr)
            result = {"daemon_failed": True}
        # daemon 返回成功 → 直接返回；否则降级
        if result.get("success") or result.get("blocked"):
            return result
        if result.get("daemon_failed") or result.get("error_code"):
            print(f"[WARN] daemon 执行失败(error_code={result.get('error_code')})，降级为直连", file=sys.stderr)
            fallback = FallbackExecutor()
            fb_result = fallback.execute(target, command, timeout, escalate, as_credential)
            fb_result["_fallback"] = True
            return fb_result
        return result

    def upload(self, target: str, files, as_credential: str = None) -> dict:
        params = {"target": target, "upload": files}
        if as_credential:
            params["as"] = as_credential
        try:
            return _normalize_daemon_resp(self._call("upload", params))
        except (TimeoutError, ConnectionError, OSError) as e:
            print(f"[WARN] daemon 无响应，降级为直连: {e}", file=sys.stderr)
            return FallbackExecutor().upload(target, files, as_credential)

    def download(self, target: str, files, as_credential: str = None) -> dict:
        params = {"target": target, "download": files}
        if as_credential:
            params["as"] = as_credential
        try:
            return _normalize_daemon_resp(self._call("download", params))
        except (TimeoutError, ConnectionError, OSError) as e:
            print(f"[WARN] daemon 无响应，降级为直连: {e}", file=sys.stderr)
            return FallbackExecutor().download(target, files, as_credential)

    def list_sessions(self) -> dict:
        return _normalize_daemon_resp(self._call("list_sessions", {}))

    def disconnect(self, target: str) -> dict:
        return _normalize_daemon_resp(self._call("disconnect", {"target": target}))

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


class FallbackExecutor(Executor):
    """降级：直连 paramiko，每次新建连接。"""

    def execute(self, target: str, command: str, timeout: int = 30,
                escalate: bool = False, as_credential: str = None) -> dict:
        session = None
        try:
            session = self._get_connection(target, as_credential)
            if escalate:
                result = session.execute_sudo(command, timeout)
            else:
                result = session.execute(command, timeout)
            return {"success": result.exit_code == 0,
                    "stdout": result.stdout, "stderr": result.stderr,
                    "exit_code": result.exit_code, "elapsed": result.elapsed,
                    "needs_escalation": result.needs_escalation,
                    "escalation_reason": result.escalation_reason,
                    "escalation_options": result.escalation_options}
        except Exception as e:
            return {"success": False, "stderr": str(e), "exit_code": 1}
        finally:
            if session:
                session.close()

    def _get_connection(self, target: str, as_credential: str = None):
        from ssh_session import SSHSession
        config = _load_config()
        environments = config.get("environments", {})

        env = environments.get(target)
        if not env:
            raise ValueError(f"未找到 target '{target}' 的环境配置，请先通过 env add 录入")

        credentials = env.get("credentials", [])
        cred_name = as_credential or env.get("default_credential", "")
        cred = next((c for c in credentials if c.get("name") == cred_name), None)
        if not cred and credentials:
            cred = credentials[0]
        if not cred:
            raise ValueError(f"环境 {target} 无有效凭证")
        session = SSHSession(
            host=env.get("host", target), port=env.get("port", 22),
            username=cred.get("username", ""), password=cred.get("password", ""),
            sudo_password=cred.get("sudo_password"),
        )
        if not session.connect():
            err = session.connect_error or {}
            raise RuntimeError(f"{err.get('code', 'CONNECT_FAILED')}: {err.get('message', '连接失败')}")
        return session

    def upload(self, target: str, files, as_credential: str = None) -> dict:
        if not isinstance(files, list):
            files = [files]
        session = None
        errors = []
        try:
            session = self._get_connection(target, as_credential)
            for f in files:
                ok, msg = session.upload(f.get("local", ""), f.get("remote", ""))
                if not ok:
                    errors.append({"local": f.get("local"), "remote": f.get("remote"), "error": msg})
            return {"success": len(errors) == 0, "total": len(files),
                    "failed": len(errors), "errors": errors}
        except Exception as e:
            return {"success": False, "stderr": str(e)}
        finally:
            if session:
                session.close()

    def download(self, target: str, files, as_credential: str = None) -> dict:
        if not isinstance(files, list):
            files = [files]
        session = None
        errors = []
        try:
            session = self._get_connection(target, as_credential)
            for f in files:
                ok, msg = session.download(f.get("remote", ""), f.get("local", ""))
                if not ok:
                    errors.append({"remote": f.get("remote"), "local": f.get("local"), "error": msg})
            return {"success": len(errors) == 0, "total": len(files),
                    "failed": len(errors), "errors": errors}
        except Exception as e:
            return {"success": False, "stderr": str(e)}
        finally:
            if session:
                session.close()

    def list_sessions(self) -> dict:
        return {"success": True, "sessions": [], "note": "降级模式下无持久会话"}

    def disconnect(self, target: str) -> dict:
        return {"success": True, "stdout": "降级模式下无需断开，连接在每次执行后自动关闭"}


# ---- get_executor ----

def get_executor():
    """按本实例的端口连接 daemon → 连不上则拉起 → 仍失败则降级。

    端口来源：daemon.port 文件（由 daemon 绑定成功后写入）。
    多 Agent 隔离：不同 skill 安装目录各有自己的端口文件，互不干扰。
    """
    # 1. 端口文件存在 → 直接连接
    port = _resolve_daemon_port()
    if port:
        sock = _try_connect_daemon(port)
        if sock:
            return DaemonExecutor(sock)

    # 2. 无端口文件或连不上 → 重试拉起 daemon（daemon 会自动选空闲端口并写文件）
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            subprocess.Popen(
                _get_daemon_cmd(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(SCRIPT_DIR)
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                port = _resolve_daemon_port()
                if port:
                    sock = _try_connect_daemon(port)
                    if sock:
                        print(f"[INFO] daemon 已启动 (尝试 {attempt} 次后成功, 端口 {port})", file=sys.stderr)
                        return DaemonExecutor(sock)
                time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] daemon 启动失败 (第 {attempt}/{MAX_RETRIES} 次): {e}", file=sys.stderr)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    # 3. 降级
    print(f"[ERROR] daemon 无法启动 (已重试 {MAX_RETRIES} 次)，降级为直连模式", file=sys.stderr)
    print("[ERROR] 每次命令将新建 SSH 连接。请检查 daemon 状态或联系管理员。", file=sys.stderr)
    return FallbackExecutor()


# ---- process_task ----

def _blocked_result(task_id: str, check: CheckResult) -> dict:
    return {
        "task_id": task_id, "success": False, "exit_code": 1,
        "stdout": "", "stderr": "", "elapsed": 0,
        "blocked": True, "level": check.level, "reason": check.reason
    }


def process_task(task: dict, guard: CommandGuard, executor: Executor) -> dict:
    task_id = task.get("task_id", "unknown")

    # inbox 文件 JSON 解析失败
    if task.get("_parse_error"):
        return {"task_id": task_id, "success": False,
                "stderr": f"task.json 不是合法 JSON: {task.get('_parse_error')}"}

    # 检测多字段冲突
    op_fields = [k for k in ["command", "upload", "download", "session", "env"] if k in task]
    if len(op_fields) > 1:
        return {"task_id": task_id, "success": False,
                "stderr": f"只能指定一种操作，当前同时存在: {', '.join(op_fields)}"}
    if len(op_fields) == 0:
        return {"task_id": task_id, "success": False,
                "stderr": "必须指定 command/upload/download/session/env 之一"}

    # 环境管理 — 本地处理
    if "env" in task:
        return _handle_env(task)

    # 会话管理 — daemon
    if "session" in task:
        session_action = task["session"]
        if session_action == "list":
            result = executor.list_sessions()
            result["task_id"] = task_id
            return result
        elif session_action == "disconnect":
            result = executor.disconnect(task.get("target", ""))
            result["task_id"] = task_id
            return result
        else:
            return {"task_id": task_id, "success": False, "error": f"未知 session 操作: {session_action}"}

    # 文件传输（先做远端路径敏感检查，批量逐个检查）
    if "upload" in task:
        u = task["upload"]
        files = u if isinstance(u, list) else [u]
        for f in files:
            check = guard.check_transfer(f.get("remote", ""))
            if not CommandGuard.can_execute(check.level, task.get("force", False)):
                return _blocked_result(task_id, check)
        result = executor.upload(task.get("target", ""), u, as_credential=task.get("as"))
        if "success" not in result:
            result["success"] = False
        result["task_id"] = task_id
        return result
    if "download" in task:
        d = task["download"]
        files = d if isinstance(d, list) else [d]
        for f in files:
            check = guard.check_transfer(f.get("remote", ""))
            if not CommandGuard.can_execute(check.level, task.get("force", False)):
                return _blocked_result(task_id, check)
        result = executor.download(task.get("target", ""), d, as_credential=task.get("as"))
        if "success" not in result:
            result["success"] = False
        result["task_id"] = task_id
        return result

    # 命令执行
    if "command" in task:
        command = task["command"]
        force = task.get("force", False)
        check_result = guard.check(command)
        if not CommandGuard.can_execute(check_result.level, force):
            return _blocked_result(task_id, check_result)
        result = executor.execute(
            task.get("target", ""), command, task.get("timeout", 30),
            escalate=task.get("escalate", False),
            as_credential=task.get("as"),
        )
        if "success" not in result:
            result["success"] = result.get("exit_code", 1) == 0
        result["task_id"] = task_id
        result["blocked"] = False
        return result

    # 无有效操作
    return {"task_id": task_id, "success": False, "stderr": "必须指定 command/upload/download/session/env 之一"}


def _handle_env(task: dict) -> dict:
    """本地处理环境管理操作。"""
    task_id = task.get("task_id", "unknown")
    env_action = task.get("env", "")
    config = _load_config()

    try:
        if env_action == "list":
            envs = config.get("environments", {})
            # 密码脱敏
            safe_envs = {}
            for ip, env in envs.items():
                safe_envs[ip] = {
                    "host": env.get("host", ip),
                    "port": env.get("port", 22),
                    "credentials": [{"name": c["name"], "username": c["username"]} for c in env.get("credentials", [])],
                    "default_credential": env.get("default_credential", ""),
                    "via": env.get("via"),
                }
            return {"task_id": task_id, "success": True, "stdout": json.dumps(safe_envs, ensure_ascii=False, indent=2)}

        elif env_action == "add":
            env_name = task.get("env_name", "")
            new_config = task.get("config", {})
            if not env_name:
                return {"task_id": task_id, "success": False, "stderr": "校验失败: env_name 不能为空"}
            if env_name in config.get("environments", {}):
                return {"task_id": task_id, "success": False,
                        "stderr": f"环境 '{env_name}' 已存在。如需修改请用 env update，或先 env remove 后再 add"}
            if not new_config.get("credentials"):
                return {"task_id": task_id, "success": False, "stderr": "校验失败: credentials 不能为空"}
            credentials = new_config.get("credentials", [])
            default_cred = new_config.get("default_credential", "")
            cred_names = [c.get("name", "") for c in credentials]
            if default_cred not in cred_names:
                return {"task_id": task_id, "success": False, "stderr": f"校验失败: default_credential '{default_cred}' 不在 credentials 中"}
            for c in credentials:
                if not c.get("name") or not c.get("username") or not c.get("password"):
                    return {"task_id": task_id, "success": False, "stderr": "校验失败: 每组凭证必须包含 name/username/password"}
            # via 校验
            via = new_config.get("via")
            if via and via not in config.get("environments", {}):
                return {"task_id": task_id, "success": False, "stderr": f"校验失败: via 指向的环境 '{via}' 未配置"}
            # host 缺省取 env_name
            new_config.setdefault("host", env_name)
            config.setdefault("environments", {})[env_name] = new_config
            _save_config(config)
            return {"task_id": task_id, "success": True, "stdout": f"已添加环境: {env_name}"}

        elif env_action == "update":
            env_name = task.get("env_name", "")
            updates = task.get("config", {})
            if env_name not in config.get("environments", {}):
                return {"task_id": task_id, "success": False, "stderr": f"环境 '{env_name}' 不存在"}
            config["environments"][env_name].update(updates)
            _save_config(config)
            return {"task_id": task_id, "success": True, "stdout": f"已更新环境: {env_name}"}

        elif env_action == "remove":
            env_name = task.get("env_name", "")
            if env_name not in config.get("environments", {}):
                return {"task_id": task_id, "success": False, "stderr": f"环境 '{env_name}' 不存在"}
            del config["environments"][env_name]
            _save_config(config)
            return {"task_id": task_id, "success": True, "stdout": f"已删除环境: {env_name}"}

        elif env_action == "credential_add":
            env_name = task.get("env_name", "")
            credential = task.get("credential", {})
            env = config.get("environments", {}).get(env_name)
            if not env:
                return {"task_id": task_id, "success": False, "stderr": f"环境 '{env_name}' 不存在"}
            cred_name = credential.get("name", "")
            if not cred_name or not credential.get("username") or not credential.get("password"):
                return {"task_id": task_id, "success": False, "stderr": "校验失败: 凭证必须包含 name/username/password"}
            existing_names = [c.get("name") for c in env.get("credentials", [])]
            if cred_name in existing_names:
                return {"task_id": task_id, "success": False, "stderr": f"凭证名称 '{cred_name}' 已存在"}
            env.setdefault("credentials", []).append(credential)
            _save_config(config)
            return {"task_id": task_id, "success": True, "stdout": f"已添加凭证: {cred_name}"}

        elif env_action == "credential_remove":
            env_name = task.get("env_name", "")
            cred_name = task.get("credential_name", "")
            env = config.get("environments", {}).get(env_name)
            if not env:
                return {"task_id": task_id, "success": False, "stderr": f"环境 '{env_name}' 不存在"}
            env["credentials"] = [c for c in env.get("credentials", []) if c.get("name") != cred_name]
            _save_config(config)
            return {"task_id": task_id, "success": True, "stdout": f"已删除凭证: {cred_name}"}

        else:
            return {"task_id": task_id, "success": False, "error": f"未知 env 操作: {env_action}"}

    except Exception as e:
        return {"task_id": task_id, "success": False, "stderr": str(e)}


def _save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass  # Windows 上 chmod 语义有限，尽力而为


# ---- print_result ----

def print_result(result: dict, task_id: str):
    if result.get("blocked"):
        print(f"[BLOCKED] [{result.get('level', '')}] {result.get('reason', '')}")
    else:
        stdout = result.get("stdout", "")
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        stderr = result.get("stderr", "")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        # 状态行：exit_code / 耗时 / 结构化错误码，便于判断结果
        status = f"[{task_id}] exit_code={result.get('exit_code', '-')} elapsed={result.get('elapsed', '-')}s"
        if result.get("error_code"):
            status += f" error={result['error_code']}"
        print(status, file=sys.stderr)

    outbox_file = f"outbox/result_{task_id}.json"
    print(f"[完整结果: {outbox_file}]")

    # 处置提示
    if result.get("blocked"):
        if result.get("level") == "warn":
            print("[提示: 向用户展示被拦截内容和原因，经用户确认后，重新写入同名任务并设置 force=true 可放行（仅当次）]")
        else:
            print("[提示: 该操作为 BLOCK 级，force=true 也无法放行。向用户说明并建议替代方案]")
    elif result.get("needs_escalation"):
        reason = result.get("escalation_reason", "权限不足")
        print(f"[提示: {reason}。可重写同名任务设置 escalate=true（需该凭证已配置 sudo_password），"
              f"或指定 \"as\": \"<凭证名>\" 换用其他凭证]", file=sys.stderr)


# ---- main ----

def main():
    ensure_dirs()

    # 兼容：--json 参数 → 写入 inbox
    if "--json" in sys.argv:
        idx = sys.argv.index("--json")
        if idx + 1 < len(sys.argv):
            json_str = sys.argv[idx + 1]
            try:
                task = json.loads(json_str)
                task.setdefault("task_id", f"cli_{int(time.time())}")
                os.makedirs(INBOX_DIR, exist_ok=True)
                with open(INBOX_FILE, "w", encoding="utf-8") as f:
                    json.dump(task, f, ensure_ascii=False)
            except json.JSONDecodeError as e:
                print(f"JSON 解析失败: {e}", file=sys.stderr)
                sys.exit(1)

    # 扫描 inbox/ 下所有 task_*.json，逐个执行
    tasks = _scan_inbox()
    if not tasks:
        print("没有待执行的任务", file=sys.stderr)
        return

    guard = CommandGuard()
    executor = get_executor()

    for task in tasks:
        task_id = task.get("task_id", "unknown")
        inbox_files_to_delete = [INBOX_DIR / f
            for f in (f"task_{task_id}.json", f"{task_id}.json", "task.json")
            if (INBOX_DIR / f).exists()]

        try:
            result = process_task(task, guard, executor)
        except Exception as e:
            result = {"task_id": task_id, "success": False,
                      "stderr": f"脚本异常: {e}", "exit_code": 1}
        write_outbox(result)
        print_result(result, task_id)

        for inbox_file in inbox_files_to_delete:
            try:
                inbox_file.unlink()
            except Exception:
                pass

    executor.close()


def ensure_dirs():
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)


def _scan_inbox() -> list:
    """扫描 inbox/ 下所有 task_*.json 文件，返回任务列表。"""
    if not INBOX_DIR.exists():
        return []
    tasks = []
    for fname in sorted(os.listdir(str(INBOX_DIR))):
        if (fname.startswith("task_") or fname == "task.json") and fname.endswith(".json"):
            fpath = INBOX_DIR / fname
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    tasks.append(json.load(f))
            except json.JSONDecodeError:
                tasks.append({"task_id": fname, "_parse_error": True})
    return tasks


def write_outbox(data: dict):
    task_id = data.get("task_id", "unknown")
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    outbox_file = OUTBOX_DIR / f"result_{task_id}.json"
    with open(outbox_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
