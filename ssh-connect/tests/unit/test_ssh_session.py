#!/usr/bin/env python3
"""P2: ssh_session.py 单元测试

测试 SSHSession 的公开方法，mock paramiko 外部依赖。
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from ssh_session import SSHSession, ExecuteResult


def _make_session(**kwargs):
    """构造 SSHSession 实例，填充必填参数默认值。"""
    defaults = {
        "host": "192.168.1.100",
        "port": 22,
        "username": "admin",
        "password": "test_pwd",
    }
    defaults.update(kwargs)
    return SSHSession(**defaults)


class TestExecute:
    """U-P2-01: execute 正常返回"""

    @patch("paramiko.SSHClient")
    def test_execute_returns_stdout(self, mock_ssh_class):
        session = _make_session()
        mock_client = mock_ssh_class.return_value

        # 构造 mock: stdin, stdout, stderr
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"hello\n"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""
        mock_stdin = MagicMock()
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)

        # 注入 mock client
        session._client = mock_client

        result = session.execute("echo hello")

        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0


class TestExecuteSudo:
    """U-P2-02, U-P2-03: execute_sudo 异常场景"""

    @patch("paramiko.SSHClient")
    def test_sudo_wrong_password(self, mock_ssh_class):
        """sudo 密码错误 → needs_escalation=True, reason 含'密码错误'"""
        session = _make_session(sudo_password="wrong_pwd")
        mock_client = mock_ssh_class.return_value

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b""
        mock_stdout.channel.recv_exit_status.return_value = 1
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b"Sorry, try again.\n"
        mock_stdin = MagicMock()
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)

        session._client = mock_client
        result = session.execute_sudo("systemctl restart nginx")

        assert result.needs_escalation == True
        assert "密码错误" in (result.escalation_reason or "")

    @patch("paramiko.SSHClient")
    def test_sudo_not_found(self, mock_ssh_class):
        """sudo 命令不存在 → needs_escalation=True, escalation_options 含 enable_root_login"""
        session = _make_session(sudo_password="pwd")
        mock_client = mock_ssh_class.return_value

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b""
        mock_stdout.channel.recv_exit_status.return_value = 1
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b"sudo: command not found\n"
        mock_stdin = MagicMock()
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)

        session._client = mock_client
        result = session.execute_sudo("whoami")

        assert result.needs_escalation == True
        assert result.escalation_options is not None
        assert "as" in result.escalation_options


class TestConnect:
    """U-P2-04: connect 认证失败"""

    @patch("paramiko.SSHClient")
    def test_connect_auth_failure(self, mock_ssh_class):
        """paramiko 抛 AuthenticationException → connect 返回 False"""
        import paramiko
        session = _make_session()
        mock_client = mock_ssh_class.return_value
        mock_client.connect.side_effect = paramiko.AuthenticationException("Auth failed")

        result = session.connect()

        assert result == False


class TestIsAlive:
    """U-P2-05: is_alive 连接存活判定"""

    @patch("paramiko.SSHClient")
    def test_is_alive_true(self, mock_ssh_class):
        """transport.is_active() 返回 True → is_alive 返回 True"""
        session = _make_session()
        mock_client = mock_ssh_class.return_value
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client.get_transport.return_value = mock_transport
        session._client = mock_client

        assert session.is_alive() == True

    @patch("paramiko.SSHClient")
    def test_is_alive_false(self, mock_ssh_class):
        """transport.is_active() 返回 False → is_alive 返回 False"""
        session = _make_session()
        mock_client = mock_ssh_class.return_value
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = False
        mock_client.get_transport.return_value = mock_transport
        session._client = mock_client

        assert session.is_alive() == False

    def test_is_alive_no_client(self):
        """未初始化 client → is_alive 返回 False"""
        session = _make_session()
        # _client 为 None
        assert session.is_alive() == False
