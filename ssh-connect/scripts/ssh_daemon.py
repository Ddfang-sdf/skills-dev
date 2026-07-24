#!/usr/bin/env python3
"""SSH 守护进程 — 后台常驻 TCP 服务器，维护 SSH 连接池

安全要点：
- 仅监听 127.0.0.1，且所有请求必须携带本机 token（daemon.token 文件）
  鉴权，防止本机其他进程/用户借用已存凭证操作服务器。
- target 只能是已录入的环境名，不接受内联凭证格式。
"""

import json
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

# 确保 scripts/ 在 sys.path 中（subprocess 启动时可能不包含）
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from ssh_session import SSHSession, ExecuteResult


# PyInstaller 打包后 __file__ 指向临时解压目录，用 sys.executable 获取实际 exe 位置
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
else:
    _exe_dir = _script_dir

# 配置文件优先找同级目录，exe 模式下回退 ../scripts/
_config_candidates = [_exe_dir, os.path.join(os.path.dirname(_exe_dir), "scripts")]
CONFIG_FILE = None
TOKEN_FILE = None
for d in _config_candidates:
    cfg = os.path.join(d, "env_config.json")
    if os.path.exists(cfg):
        CONFIG_FILE = cfg
        TOKEN_FILE = os.path.join(d, "daemon.token")
        break
if CONFIG_FILE is None:
    CONFIG_FILE = os.path.join(_exe_dir, "env_config.json")
    TOKEN_FILE = os.path.join(_exe_dir, "daemon.token")


def load_or_create_token() -> str:
    """读取本机鉴权 token；不存在则生成。run.py 使用同一文件，两侧天然一致。"""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
            if token:
                return token
    token = uuid.uuid4().hex
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass  # Windows 上 chmod 语义有限，尽力而为
    return token


# ---- 数据类 ----

@dataclass
class SessionInfo:
    session_id: str = ""
    target: str = ""
    credential_name: str = ""
    host: str = ""
    username: str = ""
    created_at: float = 0.0
    last_activity: float = 0.0
    idle_seconds: int = 0


# ---- 会话池 ----

class SessionPool:
    """线程安全的会话池。key 为 `host:credential_name`。"""

    def __init__(self):
        self._sessions: dict[str, SSHSession] = {}
        self._lock = threading.Lock()

    def get(self, session_key: str) -> Optional[SSHSession]:
        with self._lock:
            return self._sessions.get(session_key)

    def put(self, session_key: str, session: SSHSession):
        with self._lock:
            if session_key in self._sessions:
                try:
                    self._sessions[session_key].close()
                except Exception:
                    pass
            self._sessions[session_key] = session

    def remove(self, session_key: str):
        with self._lock:
            session = self._sessions.pop(session_key, None)
            if session:
                try:
                    session.close()
                except Exception:
                    pass

    def list_all(self) -> list:
        result = []
        with self._lock:
            for key, s in self._sessions.items():
                _, credential_name = key.rsplit(":", 1) if ":" in key else (key, "")
                info = SessionInfo(
                    session_id=str(uuid.uuid4()),
                    target=s.host,
                    credential_name=credential_name,
                    host=s.host,
                    username=s.username,
                    created_at=s.last_activity,
                    last_activity=s.last_activity,
                    idle_seconds=int(time.time() - s.last_activity) if s.last_activity else 0
                )
                result.append(info)
        return result

    def cleanup_idle(self, max_idle_seconds: int = 300):
        now = time.time()
        with self._lock:
            keys_to_remove = []
            for key, s in self._sessions.items():
                if s.last_activity and (now - s.last_activity) > max_idle_seconds:
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                try:
                    self._sessions[key].close()
                except Exception:
                    pass
                del self._sessions[key]

    def close_all(self):
        with self._lock:
            for s in self._sessions.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()


# ---- Daemon Server ----

class DaemonServer:
    """后台常驻 TCP 服务器，JSON-line 协议，token 鉴权。"""

    def __init__(self, config: dict, token: str):
        self.host = config.get("daemon", {}).get("host", "127.0.0.1")
        self.port = config.get("daemon", {}).get("port", 19522)
        self.session_idle_timeout = config.get("daemon", {}).get("session_idle_timeout", 300)
        self.heartbeat_interval = config.get("daemon", {}).get("heartbeat_interval", 60)
        self.pool = SessionPool()
        self.env_config = config
        self._token = token
        self._running = False
        self._server_socket: Optional[socket.socket] = None
        self._cleanup_thread: Optional[threading.Thread] = None

    # ---- 生命周期 ----

    def start(self):
        """绑定端口 → 启动 accept → 启动回收线程。"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(5)
        self._running = True

        accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        accept_thread.start()

        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

        print(f"daemon 已启动, 监听 {self.host}:{self.port}", flush=True)

    def stop(self):
        """优雅关闭。"""
        self._running = False
        self.pool.close_all()
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass

    # ---- Accept ----

    def _accept_loop(self):
        while self._running:
            try:
                self._server_socket.settimeout(1)
                conn, _ = self._server_socket.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    break

    # ---- 客户端处理 ----

    def _handle_client(self, conn: socket.socket):
        """逐行读 JSON → 鉴权 → 路由 → 写响应。"""
        buf = b""
        try:
            conn.settimeout(300)
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line.decode("utf-8"))
                        response = self._route(request)
                    except json.JSONDecodeError:
                        response = {"error": {"code": "INVALID_PARAMS", "message": "JSON 解析失败"}}
                    except Exception as e:
                        response = {"error": {"code": "INVALID_PARAMS", "message": str(e)}}
                    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- 路由 ----

    def _route(self, request: dict) -> dict:
        # 鉴权：所有方法都必须携带正确 token
        if request.get("auth") != self._token:
            return {"id": request.get("id"),
                    "error": {"code": "UNAUTHORIZED",
                              "message": "鉴权失败：请求未携带有效 token。"
                                         "若刚升级过脚本，请结束旧 daemon 进程后重试"}}

        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        handlers = {
            "ping":           self._handle_ping,
            "connect":        self._handle_connect,
            "execute":        self._handle_execute,
            "upload":         self._handle_upload,
            "download":       self._handle_download,
            "disconnect":     self._handle_disconnect,
            "list_sessions":  self._handle_list_sessions,
            "is_connected":   self._handle_is_connected,
            "shutdown":       self._handle_shutdown,
            "reset":          self._handle_reset,
        }

        handler = handlers.get(method)
        if not handler:
            return {"id": req_id, "error": {"code": "INVALID_PARAMS", "message": f"未知方法: {method}"}}

        try:
            result = handler(params)
            resp = {"id": req_id, "result": result}
            # shutdown 不需要 id
            if method == "shutdown":
                return {"result": result}
            return resp
        except Exception as e:
            return {"id": req_id, "error": {"code": "INVALID_PARAMS", "message": str(e)}}

    # ---- Handlers ----

    def _handle_ping(self, params: dict) -> dict:
        return {"status": "ok"}

    def _connect_error_result(self, session: SSHSession, host: str) -> dict:
        err = session.connect_error or {"code": "CONNECT_FAILED", "message": f"SSH 连接失败: {host}"}
        return {"error": {"code": err["code"], "message": err["message"]}}

    def _get_or_create_session(self, host, port, credential_name, username,
                               password, sudo_password, via_config):
        """查找池内活会话，否则新建。失败返回 (None, error_dict)。"""
        session_key = f"{host}:{credential_name}"
        session = self.pool.get(session_key)
        if session and session.is_alive():
            return session, None
        session = SSHSession(
            host=host, port=port,
            username=username, password=password,
            sudo_password=sudo_password,
            bastion_config=via_config
        )
        if not session.connect():
            return None, self._connect_error_result(session, host)
        self.pool.put(session_key, session)
        session.start_heartbeat(self.heartbeat_interval)
        return session, None

    def _handle_connect(self, params: dict) -> dict:
        target = params.get("target", "")
        host = params.get("host", target)
        port = params.get("port", 22)
        username = params.get("username")
        password = params.get("password")
        sudo_password = params.get("sudo_password")
        via_config = None
        if params.get("via_host"):
            via_config = {
                "host": params["via_host"],
                "port": params.get("via_port", 22),
                "username": params.get("via_username", ""),
                "password": params.get("via_password", ""),
            }

        credential_name = params.get("credential_name", "default")
        session, err = self._get_or_create_session(
            host, port, credential_name, username, password, sudo_password, via_config)
        if err:
            return err
        return {"session_id": f"{host}:{credential_name}", "host": host, "username": session.username}

    def _handle_execute(self, params: dict) -> dict:
        target = params.get("target", "")
        command = params.get("command", "")
        timeout = params.get("timeout", 30)
        escalate = params.get("escalate", False)
        as_credential = params.get("as")

        # 解析 target → host & credential
        host, credential_name, username, password, sudo_password, via_config = \
            self._resolve_target(target, as_credential)

        session, err = self._get_or_create_session(
            host, 22, credential_name, username, password, sudo_password, via_config)
        if err:
            return err

        # 执行
        if escalate:
            result = session.execute_sudo(command, timeout)
        else:
            result = session.execute(command, timeout)

        return {
            "success": result.exit_code == 0,
            "stdout": result.stdout, "stderr": result.stderr,
            "exit_code": result.exit_code, "elapsed": result.elapsed,
            "needs_escalation": result.needs_escalation,
            "escalation_reason": result.escalation_reason,
            "escalation_options": result.escalation_options,
        }

    def _handle_upload(self, params: dict) -> dict:
        target = params.get("target", "")
        local = params.get("local", "")
        remote = params.get("remote", "")
        as_credential = params.get("as")
        host, credential_name, username, password, sudo_password, via_config = \
            self._resolve_target(target, as_credential)

        session, err = self._get_or_create_session(
            host, 22, credential_name, username, password, sudo_password, via_config)
        if err:
            return err
        ok, message = session.upload(local, remote)
        return {"success": ok, "stderr": message}

    def _handle_download(self, params: dict) -> dict:
        target = params.get("target", "")
        remote = params.get("remote", "")
        local = params.get("local", "")
        as_credential = params.get("as")
        host, credential_name, username, password, sudo_password, via_config = \
            self._resolve_target(target, as_credential)

        session, err = self._get_or_create_session(
            host, 22, credential_name, username, password, sudo_password, via_config)
        if err:
            return err
        ok, message = session.download(remote, local)
        return {"success": ok, "stderr": message}

    def _handle_disconnect(self, params: dict) -> dict:
        target = params.get("target", "")
        # 移除所有匹配该 target 的 session
        to_remove = []
        for key in list(self.pool._sessions.keys()):
            if key.startswith(target + ":"):
                to_remove.append(key)
        for key in to_remove:
            self.pool.remove(key)
        return {"success": True}

    def _handle_list_sessions(self, params: dict) -> dict:
        sessions = self.pool.list_all()
        return {
            "success": True,
            "sessions": [
                {
                    "target": s.target,
                    "host": s.host,
                    "credential_name": s.credential_name,
                    "username": s.username,
                    "idle_seconds": s.idle_seconds,
                }
                for s in sessions
            ]
        }

    def _handle_is_connected(self, params: dict) -> dict:
        target = params.get("target", "")
        for key, s in self.pool._sessions.items():
            if key.startswith(target + ":"):
                return {"connected": s.is_alive(), "session_id": key}
        return {"connected": False, "session_id": None}

    def _handle_reset(self, params: dict) -> dict:
        """清空所有 session，用于测试隔离。"""
        self.pool.close_all()
        return {"success": True}

    def _handle_shutdown(self, params: dict) -> dict:
        threading.Thread(target=self._delayed_shutdown, daemon=True).start()
        return {}

    def _delayed_shutdown(self):
        time.sleep(0.1)
        self.stop()

    # ---- Target 解析 ----

    def _resolve_target(self, target: str, as_credential: str = None):
        """解析 target，返回 (host, credential_name, username, password, sudo_password, via_config)。

        target 只能是已录入的环境名（env add），不接受内联凭证格式。
        """
        env_config = self.env_config
        environments = env_config.get("environments", {})

        env = environments.get(target)
        if not env:
            raise ValueError(f"未找到 target '{target}' 的环境配置，请先通过 env add 录入")

        credentials = env.get("credentials", [])
        # 选择凭证
        cred_name = as_credential or env.get("default_credential", "")
        cred = next((c for c in credentials if c.get("name") == cred_name), None)
        if not cred and credentials:
            cred = credentials[0]  # fallback 到第一个
        if not cred:
            raise ValueError(f"环境 {target} 无有效凭证")

        username = cred.get("username", "")
        password = cred.get("password", "")
        sudo_password = cred.get("sudo_password")

        # 解析 via
        via_ip = env.get("via")
        via_config = None
        if via_ip:
            via_env = environments.get(via_ip)
            if via_env:
                via_creds = via_env.get("credentials", [])
                via_default = via_env.get("default_credential", "")
                via_cred = next((c for c in via_creds if c.get("name") == via_default), via_creds[0] if via_creds else {})
                via_config = {
                    "host": via_env.get("host", via_ip),
                    "port": via_env.get("port", 22),
                    "username": via_cred.get("username", ""),
                    "password": via_cred.get("password", ""),
                }

        host = env.get("host", target)
        return host, cred_name, username, password, sudo_password, via_config

    # ---- 空闲回收 ----

    def _cleanup_loop(self):
        """每 120s 回收空闲会话。daemon 不主动退出。"""
        while self._running:
            time.sleep(120)
            if not self._running:
                break
            self.pool.cleanup_idle(self.session_idle_timeout)


# ---- 入口 ----

def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"配置文件不存在: {CONFIG_FILE}", flush=True)
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    token = load_or_create_token()
    server = DaemonServer(config, token)
    try:
        server.start()
        while server._running:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
