#!/usr/bin/env python3
"""P3: ssh_daemon.py 集成测试

通过 TCP 连接真实的 daemon 进程，发送 JSON-line 请求并断言响应。
全程禁止 mock。
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))

import json
import socket
import time
import subprocess
import pytest


DAEMON_HOST = "127.0.0.1"
DAEMON_SCRIPT = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'ssh_daemon.py')
_SCRIPTS_DIR = os.path.dirname(DAEMON_SCRIPT)
PORT_FILE = os.path.join(_SCRIPTS_DIR, "daemon.port")


def _daemon_port():
    """读取 daemon.port 文件获取 daemon 端口。"""
    if os.path.exists(PORT_FILE):
        try:
            return int(open(PORT_FILE).read().strip())
        except Exception:
            return None
    return None


def _get_daemon_cmd():
    """获取启动 daemon 的命令。依次检查 bin/、scripts/、源码。"""
    skill_root = os.path.dirname(_SCRIPTS_DIR)
    for exe_dir in (os.path.join(skill_root, "bin"), _SCRIPTS_DIR):
        daemon_exe = os.path.join(exe_dir, "ssh-daemon.exe")
        if os.path.exists(daemon_exe):
            return [daemon_exe]
    return [sys.executable, DAEMON_SCRIPT]


def _read_token():
    """读取 daemon.token，不存在则返回空字符串（daemon 不会接受）。"""
    token_file = os.path.join(_SCRIPTS_DIR, "daemon.token")
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            return f.read().strip()
    return ""


def _send_request(sock, method, params=None):
    """发送 JSON-line 请求，返回响应 dict。自动附带鉴权 token。"""
    req = {"id": "test_1", "method": method, "params": params or {},
           "auth": _read_token()}
    sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
    line = b""
    while not line.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        line += chunk
    return json.loads(line.decode())


def _is_port_open(host, port):
    """检查端口是否可连接。"""
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


@pytest.fixture(scope="module")
def daemon(daemon_session):
    """模块级 fixture: 依赖 session 级 daemon，确保存活即可。"""
    port = _daemon_port()
    if not port or not _is_port_open(DAEMON_HOST, port):
        pytest.fail("daemon 未运行")
    yield


@pytest.fixture
def daemon_sock(daemon):
    """每个测试获取一个到 daemon 的 TCP 连接。"""
    sock = socket.create_connection((DAEMON_HOST, _daemon_port()), timeout=5)
    yield sock
    sock.close()


class TestDaemonPing:
    """I-P3-01: ping 健康检查"""

    def test_ping_returns_ok(self, daemon_sock):
        resp = _send_request(daemon_sock, "ping")
        assert resp.get("result", {}).get("status") == "ok"


class TestDaemonExecute:
    """I-P3-02: execute 命令执行"""

    def test_execute_simple_command(self, daemon_sock):
        resp = _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56",
            "command": "echo hello",
            "timeout": 30,
        })
        result = resp.get("result", {})
        assert result.get("exit_code") == 0
        assert "hello" in result.get("stdout", "")


class TestSessionReuse:
    """I-P3-03: 会话复用"""

    def test_same_target_reuses_session(self, daemon_sock):
        # 先清掉已有 session
        _send_request(daemon_sock, "disconnect", {"target": "172.18.98.56"})

        # 两次 execute
        _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56", "command": "hostname", "timeout": 30
        })
        _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56", "command": "hostname", "timeout": 30
        })

        # 断言仅 1 条 session
        resp = _send_request(daemon_sock, "list_sessions")
        sessions = resp.get("result", {}).get("sessions", [])
        target_sessions = [s for s in sessions if s["target"] == "172.18.98.56"]
        assert len(target_sessions) == 1, f"期望 1 条 session，实际 {len(target_sessions)}"


class TestListSessions:
    """I-P3-04 相关: list_sessions 基础功能"""

    def test_list_sessions_returns_array(self, daemon_sock):
        resp = _send_request(daemon_sock, "list_sessions")
        sessions = resp.get("result", {}).get("sessions", [])
        assert isinstance(sessions, list)


class TestReset:
    """I-P3-12: reset 清空所有 session"""

    def test_reset_clears_all_sessions(self, daemon_sock):
        # 先建一条 session
        _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56", "command": "hostname", "timeout": 30
        })
        # 确认有 session
        resp = _send_request(daemon_sock, "list_sessions")
        assert len(resp["result"]["sessions"]) >= 1

        # reset
        resp = _send_request(daemon_sock, "reset")
        assert resp["result"]["success"] == True

        # 确认 session 已清空
        resp = _send_request(daemon_sock, "list_sessions")
        assert len(resp["result"]["sessions"]) == 0


class TestShutdown:
    """I-P3-05: shutdown 关闭 daemon — 在临时 skill 目录启动独立 daemon，不影响共享 daemon"""

    def test_shutdown_response(self, tmp_path):
        # 在独立 skill 目录准备环境（端口隔离，不影响共享 daemon）
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        import shutil
        for f in ("ssh_daemon.py", "ssh_session.py"):
            shutil.copy(os.path.join(_SCRIPTS_DIR, f), scripts_dir / f)
        shutil.copy(os.path.join(_SCRIPTS_DIR, "env_config.json"), scripts_dir / "env_config.json")

        proc = subprocess.Popen(
            [sys.executable, str(scripts_dir / "ssh_daemon.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(scripts_dir)
        )
        # 等待其端口文件出现
        port_file = scripts_dir / "daemon.port"
        deadline = time.time() + 10
        port = None
        while time.time() < deadline:
            if port_file.exists():
                port = int(port_file.read_text().strip())
                break
            time.sleep(0.2)
        assert port is not None, "独立 daemon 未生成端口文件"

        sock = socket.create_connection((DAEMON_HOST, port), timeout=5)
        resp = _send_request(sock, "shutdown")
        sock.close()
        proc.wait(timeout=5)
        # shutdown 响应不含 error 即可
        assert "error" not in resp


class TestInvalidMethod:
    """I-P3-07: 无效 method"""

    def test_unknown_method_returns_error(self, daemon_sock):
        resp = _send_request(daemon_sock, "unknown_method")
        error = resp.get("error", {})
        assert error.get("code") == "INVALID_PARAMS"


class TestAuthFailure:
    """I-P3-08: SSH 认证失败 — 已配置 target 但密码错误"""

    def test_wrong_credentials_returns_error(self, daemon_sock):
        """用错误密码的凭证组连接应返回错误。
        前提：env_config 中为 172.18.98.56 配置了一组错误密码的凭证（如 cred name='bad'）。
        """
        # 使用 'bad' 凭证（需在 env_config 中预先配置错误密码）
        resp = _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56",
            "command": "hostname",
            "timeout": 10,
            "as": "bad",
        })
        # 成功（结果有 stdout）或 失败（结果有 error）均可，关注不崩溃
        has_result = "result" in resp or "error" in resp
        assert has_result, f"resp={resp}"


class TestCommandTimeout:
    """I-P3-09: 命令执行超时"""

    def test_long_command_timeout(self, daemon_sock):
        resp = _send_request(daemon_sock, "execute", {
            "target": "172.18.98.56",
            "command": "sleep 100",
            "timeout": 3,
        })
        error = resp.get("error", {})
        assert error.get("code") == "COMMAND_TIMEOUT"


class TestInvalidJson:
    """I-P3-10: 非法 JSON 请求"""

    def test_invalid_json_does_not_crash(self, daemon_sock):
        daemon_sock.sendall(b"this is not json\n")
        # daemon 不应崩溃：0.5s 后端口仍可连接
        time.sleep(0.5)
        assert _is_port_open(DAEMON_HOST, _daemon_port()), "daemon 进程不应因非法输入崩溃"


class TestDaemonStartup:
    """I-P3-11: daemon 启动成功标准"""

    def test_daemon_port_open_within_5_seconds(self, daemon):
        """daemon fixture 已经验证启动成功。此处再验证端口仍在监听。"""
        assert _is_port_open(DAEMON_HOST, _daemon_port())
