#!/usr/bin/env python3
"""P2: ssh_session.py 集成测试

调用 SSHSession 的公开方法，全程禁止 mock。
链路必须穿透到数据层（真实 SSH 连接、文件系统）。
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))

import time
import tempfile
import pytest
from ssh_session import SSHSession, ExecuteResult


# 测试环境配置（需在实际环境中替换）
TEST_HOST = os.environ.get("SSH_TEST_HOST", "192.168.1.100")
TEST_PORT = int(os.environ.get("SSH_TEST_PORT", "22"))
TEST_USER = os.environ.get("SSH_TEST_USER", "admin")
TEST_PASS = os.environ.get("SSH_TEST_PASS", "")
TEST_SUDO_PASS = os.environ.get("SSH_TEST_SUDO_PASS", "")


def _make_session(**kwargs):
    defaults = {
        "host": TEST_HOST,
        "port": TEST_PORT,
        "username": TEST_USER,
        "password": TEST_PASS,
    }
    defaults.update(kwargs)
    return SSHSession(**defaults)


@pytest.mark.integration
class TestDirectExecute:
    """I-P2-01: 直连 — execute"""

    def test_execute_returns_stdout(self):
        session = _make_session()
        assert session.connect() == True
        result = session.execute("echo hello_integration")
        assert result.exit_code == 0
        assert "hello_integration" in result.stdout
        session.close()


@pytest.mark.integration
class TestSFTP:
    """I-P2-02: 直连 — upload/download"""

    def test_upload_download_roundtrip(self):
        content = "sftp_test_content_42"
        # 写临时本地文件
        tmp_local = os.path.join(tempfile.gettempdir(), "sftp_upload_test.txt")
        tmp_dl = os.path.join(tempfile.gettempdir(), "sftp_download_test.txt")
        with open(tmp_local, "w") as f:
            f.write(content)

        session = _make_session()
        assert session.connect() == True

        # 上传
        ok, err = session.upload(tmp_local, "/tmp/sftp_roundtrip_test.txt")
        assert ok, f"upload failed: {err}"

        # 下载
        ok, err = session.download("/tmp/sftp_roundtrip_test.txt", tmp_dl)
        assert ok, f"download failed: {err}"

        with open(tmp_dl, "r") as f:
            assert f.read() == content

        session.close()
        os.remove(tmp_local)
        os.remove(tmp_dl)


@pytest.mark.integration
class TestSudoEscalation:
    """I-P2-04: sudo 提权"""

    def test_execute_sudo_with_correct_password(self):
        if not TEST_SUDO_PASS:
            pytest.skip("未配置 SSH_TEST_SUDO_PASS")
        session = _make_session(sudo_password=TEST_SUDO_PASS)
        assert session.connect() == True

        # 普通执行应失败（权限不足）
        r1 = session.execute("touch /root/test_sudo_integration")
        # escalate=true 应成功
        r2 = session.execute_sudo("touch /root/test_sudo_integration")
        assert r2.exit_code == 0

        session.close()


@pytest.mark.integration
class TestHeartbeat:
    """I-P2-05: 心跳保活"""

    def test_connection_alive_after_idle(self):
        session = _make_session()
        assert session.connect() == True
        session.start_heartbeat(interval=10)

        time.sleep(25)  # 等待至少 2 次心跳
        assert session.is_alive() == True

        result = session.execute("echo still_alive")
        assert result.exit_code == 0
        assert "still_alive" in result.stdout

        session.stop_heartbeat()
        session.close()


@pytest.mark.integration
class TestBastionConnect:
    """I-P2-06: 跳板机连接失败"""

    def test_unreachable_bastion_returns_false(self):
        session = _make_session(
            host="10.0.1.5",
            bastion_config={
                "host": "1.2.3.4",
                "port": 22,
                "username": "nobody",
                "password": "nope"
            }
        )
        result = session.connect(timeout=5)
        assert result == False


@pytest.mark.integration
class TestTCPTimeout:
    """I-P2-07: SSH 连接超时（TCP 层不可达）"""

    def test_unreachable_host_timeout(self):
        session = SSHSession(
            host="10.255.255.1",
            port=22,
            username="root",
            password="x"
        )
        t0 = time.time()
        result = session.connect(timeout=5)
        elapsed = time.time() - t0
        assert result == False
        # 验证不会无限 hang，放宽至 15s
        assert elapsed < 15, f"连接超时耗时 {elapsed}s，超过 15s 上限"
