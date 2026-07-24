"""pytest 共享配置和 fixtures"""
import pytest
import os
import sys
import socket
import subprocess
import time

# 将 scripts 目录加入 path
_scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 19522
_skill_root = os.path.dirname(_scripts_dir)


def _get_daemon_cmd():
    """获取启动 daemon 的命令。依次检查 bin/、scripts/、源码。"""
    for exe_dir in (os.path.join(_skill_root, "bin"), _scripts_dir):
        daemon_exe = os.path.join(exe_dir, "ssh-daemon.exe")
        if os.path.exists(daemon_exe):
            return [daemon_exe]
    return [sys.executable, os.path.join(_scripts_dir, "ssh_daemon.py")]


def _is_daemon_alive():
    try:
        s = socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=1)
        s.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def daemon_session():
    """Session 级 fixture: 确保整个测试套件期间 daemon 始终存活。"""
    if _is_daemon_alive():
        yield
        return

    proc = subprocess.Popen(_get_daemon_cmd(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if _is_daemon_alive():
            break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("daemon 启动超时，无法运行测试")

    yield  # 所有测试结束

    # 清理
    try:
        import json
        token_file = os.path.join(_scripts_dir, "daemon.token")
        token = open(token_file).read().strip() if os.path.exists(token_file) else ""
        s = socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=2)
        s.sendall((json.dumps({"id": "ci", "method": "shutdown", "params": {}, "auth": token}) + "\n").encode())
        s.close()
    except Exception:
        pass
    proc.wait(timeout=5)


def pytest_configure(config):
    """注册自定义 markers"""
    config.addinivalue_line("markers", "integration: 集成测试，需要实际基础设施")
    config.addinivalue_line("markers", "blackbox: 黑盒测试，仅通过 shell 执行")
