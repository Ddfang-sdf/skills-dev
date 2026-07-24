#!/usr/bin/env python3
"""P4: run.py 黑盒测试

模拟 AI 行为：写 inbox 文件 → 命令行执行 run.py → 读 outbox 文件 → 断言。
禁止 import 任何被测模块。全程通过 subprocess + 文件系统操作。
"""

import sys
import os
import json
import time
import socket
import subprocess
import tempfile
import shutil
import pytest


# 路径常量
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
RUN_PY = os.path.join(SCRIPTS_DIR, 'run.py')
INBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'inbox')
OUTBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'outbox')
INBOX_FILE = os.path.join(INBOX_DIR, 'task.json')
OUTBOX_FILE = os.path.join(OUTBOX_DIR, 'result.json')


def _write_inbox(data: dict):
    """写入 inbox/task.json。"""
    os.makedirs(INBOX_DIR, exist_ok=True)
    with open(INBOX_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def _read_outbox() -> dict:
    """读取 outbox/result.json。"""
    with open(OUTBOX_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def _run() -> subprocess.CompletedProcess:
    """执行 run.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, RUN_PY],
        capture_output=True, text=True, timeout=60,
        cwd=SCRIPTS_DIR
    )


def _cleanup():
    """清理 inbox 和 outbox。"""
    for d in [INBOX_DIR, OUTBOX_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)


@pytest.fixture(autouse=True)
def setup_teardown():
    """每个用例前后清理 inbox/outbox，并重置 daemon 状态。"""
    _cleanup()
    # 重置 daemon session pool，消除跨测试耦合
    _reset_daemon()
    yield
    _cleanup()


def _read_token():
    """读取 daemon.token 获取鉴权 token。"""
    token_file = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'daemon.token')
    if os.path.exists(token_file):
        with open(token_file, 'r') as f:
            return f.read().strip()
    return ""


def _reset_daemon():
    """向 daemon 发送 reset 请求，清空所有 session。忽略连接失败（daemon 未运行时跳过）。"""
    try:
        sock = socket.create_connection(("127.0.0.1", 19522), timeout=2)
        req = {"id": "reset", "method": "reset", "params": {}, "auth": _read_token()}
        sock.sendall((json.dumps(req) + "\n").encode())
        sock.settimeout(2)
        sock.recv(1024)
        sock.close()
    except Exception:
        pass


# ============================================================
# 正常场景
# ============================================================

class TestCommandExecution:
    """B-P4-01: 完整 Write → Run → Read 链路"""

    def test_simple_command(self):
        _write_inbox({
            "task_id": "task_1",
            "target": "172.18.98.56",
            "command": "echo hello_blackbox",
            "timeout": 30,
        })
        proc = _run()
        result = _read_outbox()

        assert "hello_blackbox" in proc.stdout
        assert "[完整结果: outbox/result.json]" in proc.stdout
        assert result["success"] == True
        assert result["exit_code"] == 0
        assert "hello_blackbox" in result["stdout"]
        # inbox 文件被删除
        assert not os.path.exists(INBOX_FILE)


class TestFileUpload:
    """B-P4-02: 文件上传"""

    def test_upload_file(self):
        # 创建临时测试文件
        tmp_file = os.path.join(tempfile.gettempdir(), "upload_test.txt")
        with open(tmp_file, "w") as f:
            f.write("upload_content")

        _write_inbox({
            "task_id": "task_2",
            "target": "172.18.98.56",
            "upload": {
                "local": tmp_file,
                "remote": "/tmp/blackbox_upload_test.txt"
            }
        })
        _run()
        result = _read_outbox()
        assert result["success"] == True
        os.remove(tmp_file)


class TestFileDownload:
    """B-P4-06B: 文件下载"""

    def test_download_file(self):
        tmp_dl = os.path.join(tempfile.gettempdir(), "dl_result.txt")
        _write_inbox({
            "task_id": "task_6",
            "target": "172.18.98.56",
            "download": {
                "remote": "/tmp/blackbox_dl_test.txt",
                "local": tmp_dl
            }
        })
        _run()
        result = _read_outbox()
        assert result["success"] == True


class TestDangerousCommandBlock:
    """B-P4-03: 危险命令 BLOCK"""

    def test_rm_rf_root_blocked(self):
        _write_inbox({
            "task_id": "task_1",
            "target": "172.18.98.56",
            "command": "rm -rf /",
        })
        proc = _run()
        result = _read_outbox()

        assert "[BLOCKED]" in proc.stdout
        assert result["blocked"] == True
        assert result["level"] == "block"


class TestDangerousCommandForce:
    """B-P4-04: WARN 命令 + force=true 放行"""

    def test_rm_with_force(self):
        _write_inbox({
            "task_id": "task_1",
            "target": "172.18.98.56",
            "command": "rm -rf /tmp/blackbox_test_dir",
            "force": True,
        })
        _run()
        result = _read_outbox()
        assert result["blocked"] == False


class TestSessionManagement:
    """B-P4-05: 会话管理"""

    def test_list_sessions(self):
        _write_inbox({"task_id": "task_3", "session": "list"})
        _run()
        result = _read_outbox()
        assert result["success"] == True

    def test_disconnect(self):
        _write_inbox({"task_id": "task_4", "session": "disconnect", "target": "172.18.98.56"})
        _run()
        result = _read_outbox()
        assert result["success"] == True


class TestEnvManagement:
    """B-P4-06: 环境管理 — add/list/remove"""

    def test_env_add_list_remove(self):
        # add
        _write_inbox({
            "task_id": "env_01", "env": "add", "env_name": "192.168.1.200",
            "config": {
                "host": "192.168.1.200",
                "credentials": [{"name": "admin", "username": "a", "password": "p"}],
                "default_credential": "admin"
            }
        })
        _run()
        r1 = _read_outbox()
        assert r1["success"] == True

        # list
        _write_inbox({"task_id": "env_02", "env": "list"})
        _run()
        r2 = _read_outbox()
        assert r2["success"] == True

        # remove
        _write_inbox({"task_id": "env_03", "env": "remove", "env_name": "192.168.1.200"})
        _run()
        r3 = _read_outbox()
        assert r3["success"] == True


class TestAsCredential:
    """B-P4-06C: as 字段切换凭证"""

    def test_as_root_credential(self):
        _write_inbox({
            "task_id": "task_7",
            "target": "172.18.98.56",
            "command": "whoami",
            "as": "root",
        })
        _run()
        result = _read_outbox()
        assert result["success"] == True
        # whoami 应返回 root（使用了 root 凭证）
        assert result["stdout"].strip() == "root"


class TestMultiCredentialSessions:
    """B-P4-06D: 多凭证独立 session"""

    def test_two_credentials_two_sessions(self):
        # admin session
        _write_inbox({"task_id": "t1", "target": "172.18.98.56", "command": "whoami", "as": "admin"})
        _run()
        # root session
        _write_inbox({"task_id": "t2", "target": "172.18.98.56", "command": "whoami", "as": "root"})
        _run()
        # 验证两个 session
        _write_inbox({"task_id": "t3", "session": "list"})
        _run()
        result = _read_outbox()
        sessions = result.get("sessions", [])
        target_sessions = [s for s in sessions if s["host"] == "172.18.98.56"]
        assert len(target_sessions) == 2


class TestEnvUpdate:
    """B-P4-06E: env update"""

    def test_env_update(self):
        # 先 add
        _write_inbox({
            "task_id": "eu1", "env": "add", "env_name": "192.168.1.201",
            "config": {
                "host": "192.168.1.201",
                "credentials": [{"name": "a", "username": "a", "password": "p"}],
                "default_credential": "a"
            }
        })
        _run()
        # update
        _write_inbox({
            "task_id": "eu2", "env": "update", "env_name": "192.168.1.201",
            "config": {"port": 2222}
        })
        _run()
        result = _read_outbox()
        assert result["success"] == True


class TestCredentialAddRemove:
    """B-P4-06F: credential_add / credential_remove"""

    def test_credential_add_remove(self):
        # add env
        _write_inbox({
            "task_id": "cr1", "env": "add", "env_name": "192.168.1.202",
            "config": {
                "host": "192.168.1.202",
                "credentials": [{"name": "a", "username": "a", "password": "p"}],
                "default_credential": "a"
            }
        })
        _run()
        # credential_add
        _write_inbox({
            "task_id": "cr2", "env": "credential_add", "env_name": "192.168.1.202",
            "credential": {"name": "deploy", "username": "d", "password": "dp"}
        })
        _run()
        r1 = _read_outbox()
        assert r1["success"] == True
        # credential_remove
        _write_inbox({
            "task_id": "cr3", "env": "credential_remove",
            "env_name": "192.168.1.202", "credential_name": "deploy"
        })
        _run()
        r2 = _read_outbox()
        assert r2["success"] == True


class TestEscalate:
    """B-P4-11: escalate 提权"""

    @pytest.mark.skip(reason="需要非 root 用户环境，Alpine WSL 默认 root")
    def test_escalate_true_sudo_execution(self):
        # 第一步: escalate=false — 权限不足
        _write_inbox({
            "task_id": "esc_1", "target": "172.18.98.56",
            "command": "touch /root/blackbox_escalate_test", "escalate": False
        })
        _run()
        r1 = _read_outbox()
        assert r1["exit_code"] != 0

        # 第二步: escalate=true — 提权成功
        _write_inbox({
            "task_id": "esc_1", "target": "172.18.98.56",
            "command": "touch /root/blackbox_escalate_test", "escalate": True
        })
        _run()
        r2 = _read_outbox()
        assert r2["exit_code"] == 0
        assert r2["success"] == True


class TestEnableRootLogin:
    """B-P4-18: enable_root_login 完整流程"""

    def test_enable_root_login_flow(self):
        """完整提权流程: 权限不足 → 确认 → 改 sshd_config → root 重连成功"""
        if not os.environ.get("SSH_TEST_ENABLE_ROOT_LOGIN"):
            pytest.skip("设置 SSH_TEST_ENABLE_ROOT_LOGIN=1 以运行此测试（会修改远程 sshd_config）")

        # 步骤 1: escalate=false 执行需要 root 的命令
        _write_inbox({
            "task_id": "root_1", "target": "172.18.98.56",
            "command": "cat /etc/shadow", "escalate": False
        })
        proc1 = _run()
        result1 = _read_outbox()
        # 预期失败（权限不足）
        assert result1["exit_code"] != 0, f"步骤1预期权限不足，实际 exit_code={result1['exit_code']}"

        # 步骤 2: 确认 escalation_options 含 enable_root_login
        # escalation_options 在 result 中由脚本返回
        assert "enable_root_login" in str(result1)

        # 步骤 3: AI 确认后，触发 enable_root_login — 这里直接调用 daemon RPC
        # （实际生产环境中由 AI 通过 inbox action 触发，测试环境直接验证效果）
        import socket, json as _json
        sock = socket.create_connection(("127.0.0.1", 19522), timeout=5)
        sock.sendall((_json.dumps({
            "id": "enable_root", "method": "connect",
            "params": {"target": "172.18.98.56", "host": "172.18.98.56",
                       "username": "root", "password": os.environ.get("SSH_TEST_ROOT_PASS", ""),
                       "escalate_action": "enable_root_login"}
        }) + "\n").encode())
        resp = _json.loads(sock.recv(4096).decode())
        sock.close()
        assert "result" in resp, f"enable_root_login 失败: {resp}"

        # 步骤 4: root 重连后执行原命令 → 成功
        _write_inbox({
            "task_id": "root_1", "target": "172.18.98.56",
            "command": "cat /etc/shadow", "as": "root"
        })
        _run()
        result2 = _read_outbox()
        assert result2["exit_code"] == 0, f"步骤4预期 root 执行成功，实际 {result2}"


# ============================================================
# 异常场景
# ============================================================

class TestEmptyInbox:
    """B-P4-07: inbox 为空"""

    def test_empty_inbox(self):
        proc = _run()
        assert proc.returncode == 0
        assert "没有待执行的任务" in proc.stderr


class TestInvalidJson:
    """B-P4-08: JSON 格式错误"""

    def test_invalid_json_in_inbox(self):
        os.makedirs(INBOX_DIR, exist_ok=True)
        with open(INBOX_FILE, 'w') as f:
            f.write("{bad json")
        proc = _run()
        result = _read_outbox()
        assert result["success"] == False


class TestTargetNotFound:
    """B-P4-09: target 无配置"""

    def test_unconfigured_target(self):
        _write_inbox({
            "task_id": "task_1", "target": "1.2.3.4", "command": "hostname"
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestDaemonFallback:
    """B-P4-10: daemon 降级"""

    def test_daemon_unavailable_fallback(self):
        """daemon 不可用且无法启动 → 降级为直连，保留错误原因"""
        if not os.environ.get("SSH_TEST_NO_DAEMON"):
            pytest.skip("设置 SSH_TEST_NO_DAEMON=1 并确保 daemon 已停止以运行此测试")

        _write_inbox({
            "task_id": "task_fb", "target": "172.18.98.56",
            "command": "echo fallback_test"
        })
        proc = _run()
        result = _read_outbox()

        # stderr 含重试失败原因 + 降级提示
        assert "降级为直连模式" in proc.stderr
        assert "联系管理员" in proc.stderr

        # 命令仍能通过直连执行成功
        assert result["success"] == True
        assert "fallback_test" in result["stdout"]


class TestMultipleFieldsConflict:
    """B-P4-12: 多个顶层字段同时存在"""

    def test_command_and_upload_together(self):
        _write_inbox({
            "task_id": "x", "target": "172.18.98.56",
            "command": "ls",
            "upload": {"local": "/x", "remote": "/x"}
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False
        assert "只能指定一种操作" in result.get("stderr", result.get("error", ""))


class TestNoOperationField:
    """B-P4-13: 无任何顶层操作字段"""

    def test_no_operation_field(self):
        _write_inbox({"task_id": "x", "target": "172.18.98.56"})
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestEnvRemoveNonexistent:
    """B-P4-14: env remove 不存在"""

    def test_remove_nonexistent_env(self):
        _write_inbox({"task_id": "x", "env": "remove", "env_name": "1.2.3.4"})
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestDisconnectNonexistent:
    """B-P4-15: session disconnect 不存在"""

    def test_disconnect_nonexistent_target(self):
        _write_inbox({"task_id": "x", "session": "disconnect", "target": "1.2.3.4"})
        _run()
        result = _read_outbox()
        # 幂等可返回 success 或 false，只要不抛异常
        assert "success" in result


class TestEnvEmptyCredentials:
    """B-P4-16: credentials 为空"""

    def test_empty_credentials(self):
        _write_inbox({
            "task_id": "x", "env": "add", "env_name": "10.10.10.10",
            "config": {
                "host": "10.10.10.10",
                "credentials": [],
                "default_credential": "x"
            }
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestEnvDefaultCredentialMismatch:
    """B-P4-17: default_credential 不匹配"""

    def test_default_credential_not_in_credentials(self):
        _write_inbox({
            "task_id": "x", "env": "add", "env_name": "10.10.10.10",
            "config": {
                "host": "10.10.10.10",
                "credentials": [{"name": "admin", "username": "a", "password": "p"}],
                "default_credential": "not_exist"
            }
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestSSHTimeout:
    """B-P4-19: SSH 连接超时"""

    @pytest.mark.skip(reason="依赖 OS TCP 超时行为，CI 环境不稳定")
    def test_ssh_connection_timeout(self):
        _write_inbox({
            "task_id": "task_1", "target": "10.255.255.1",
            "command": "hostname", "timeout": 5
        })
        proc = _run()
        result = _read_outbox()
        # 不可达 IP：要么连接超时，要么返回连接失败
        assert result["success"] == False
        assert ("超时" in str(result) or "CONNECT_FAILED" in str(result)
                or "超时" in proc.stderr or "超时" in proc.stdout)


class TestSFTPErrors:
    """B-P4-20/B-P4-21: SFTP 错误"""

    def test_upload_to_nonexistent_path(self):
        _write_inbox({
            "task_id": "t2", "target": "172.18.98.56",
            "upload": {"local": "/tmp/fake.txt", "remote": "/nonexist/f.txt"}
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False

    def test_download_nonexistent_file(self):
        _write_inbox({
            "task_id": "t3", "target": "172.18.98.56",
            "download": {"remote": "/nonexist_file.txt", "local": "/tmp/dummy.txt"}
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestViaNotConfigured:
    """B-P4-22: via 指向未配置 IP"""

    def test_via_unconfigured_ip(self):
        _write_inbox({
            "task_id": "x", "env": "add", "env_name": "10.0.1.5",
            "config": {
                "host": "10.0.1.5",
                "credentials": [{"name": "a", "username": "a", "password": "p"}],
                "default_credential": "a",
                "via": "9.9.9.9"
            }
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestCredentialNameDuplicate:
    """B-P4-23: credential name 重复"""

    def test_duplicate_credential_name(self):
        # add env
        _write_inbox({
            "task_id": "d1", "env": "add", "env_name": "10.10.10.11",
            "config": {
                "host": "10.10.10.11",
                "credentials": [{"name": "admin", "username": "a", "password": "p"}],
                "default_credential": "admin"
            }
        })
        _run()
        # add duplicate name
        _write_inbox({
            "task_id": "d2", "env": "credential_add", "env_name": "10.10.10.11",
            "credential": {"name": "admin", "username": "x", "password": "y"}
        })
        _run()
        result = _read_outbox()
        assert result["success"] == False


class TestStderrOutput:
    """验证 stderr 含必要的错误信息"""

    def test_error_info_in_stderr(self):
        _write_inbox({
            "task_id": "task_1", "target": "1.2.3.4", "command": "hostname"
        })
        proc = _run()
        result = _read_outbox()
        # 无论 daemon 运行与否，未知 target 都应产生错误
        has_error = (len(proc.stderr) > 0
                     or result.get("success") == False
                     or result.get("code") is not None)
        assert has_error, f"stderr='{proc.stderr}', result={result}"
