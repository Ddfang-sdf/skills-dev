#!/usr/bin/env python3
"""SSH 会话模块

封装单个 SSH 会话的：连接建立（含跳板机穿透）、命令执行、sudo 提权、
文件传输、心跳保活。

安全要点：
- 主机密钥采用 accept-new 语义：首次连接接受并记录指纹（打印到 stderr），
  之后严格校验，密钥变更会拒绝连接（防 MITM）。
- connect() 失败时通过 self.connect_error 返回结构化错误码，
  调用方不应靠猜 stderr 判断失败原因。
- 本模块不提供"启用 root 登录"之类的功能。需要 root 时请使用
  sudo 提权（escalate）或 root 凭证组（as）。
"""

import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import paramiko

# 路径：PyInstaller 打包后 __file__ 指向临时解压目录，用 sys.executable 获取实际位置
if getattr(sys, 'frozen', False):
    _exe_dir = Path(sys.executable).parent
else:
    _exe_dir = Path(__file__).parent

# known_hosts 优先放 scripts/，exe 模式下回退 bin/
_config_dirs = [_exe_dir, _exe_dir.parent / "scripts"]
KNOWN_HOSTS_FILE = _exe_dir / "known_hosts"  # 默认
for d in _config_dirs:
    if (d / "env_config.json").exists() or d.name == "scripts":
        KNOWN_HOSTS_FILE = d / "known_hosts"
        break


@dataclass
class ExecuteResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    elapsed: float = 0.0
    needs_escalation: bool = False
    escalation_reason: Optional[str] = None
    escalation_options: list = field(default_factory=list)


class _AcceptNewHostKeyPolicy(paramiko.AutoAddPolicy):
    """accept-new 策略：接受未知主机密钥并记录，指纹打印到 stderr。

    注意：密钥"变更"不属于 missing，paramiko 会在策略之前抛出
    BadHostKeyException，天然防替换攻击。
    """

    def __init__(self):
        super().__init__()
        self.accepted = []

    def missing_host_key(self, client, hostname, key):
        fp = key.get_fingerprint().hex()
        self.accepted.append((hostname, key.get_name(), fp))
        print(f"[HOSTKEY] 首次连接 {hostname}，信任新主机密钥 "
              f"({key.get_name()}, MD5 指纹 {fp})", file=sys.stderr)
        super().missing_host_key(client, hostname, key)


def _new_client() -> tuple:
    """创建加载了 known_hosts 的 SSHClient，返回 (client, policy)。"""
    client = paramiko.SSHClient()
    if KNOWN_HOSTS_FILE.exists():
        try:
            client.load_host_keys(str(KNOWN_HOSTS_FILE))
        except Exception as e:
            print(f"[HOSTKEY] 读取 known_hosts 失败（忽略并继续）: {e}", file=sys.stderr)
    policy = _AcceptNewHostKeyPolicy()
    client.set_missing_host_key_policy(policy)
    return client, policy


def _save_known_hosts(client):
    try:
        client.save_host_keys(str(KNOWN_HOSTS_FILE))
    except Exception as e:
        print(f"[HOSTKEY] 写入 known_hosts 失败: {e}", file=sys.stderr)


class SSHSession:
    """封装单个 SSH 会话的全部操作。"""

    # 连接失败的结构化错误码
    ERR_AUTH = "AUTH_FAILED"            # 认证失败（用户名/密码错误）
    ERR_TIMEOUT = "TIMEOUT"             # 连接超时
    ERR_UNREACHABLE = "UNREACHABLE"     # 网络不可达/拒绝连接/DNS 失败
    ERR_HOSTKEY = "HOSTKEY_CHANGED"     # 主机密钥与记录不符（疑似 MITM 或机器重装）
    ERR_SSH = "SSH_ERROR"               # 其他 SSH 协议错误
    ERR_UNKNOWN = "CONNECT_FAILED"      # 未分类失败

    def __init__(self, host: str, port: int = 22, username: str = None,
                 password: str = None, sudo_password: str = None,
                 bastion_config: dict = None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sudo_password = sudo_password
        self._bastion_config = bastion_config
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._bastion_client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_running = False
        self._stale = False
        self.last_activity: float = 0.0
        self.connect_error: Optional[dict] = None  # {"code": ..., "message": ...}

    # ---- 连接 ----

    def connect(self, timeout: int = 30) -> bool:
        """建立 SSH 连接。成功返回 True；失败返回 False 并设置 connect_error。"""
        self.connect_error = None
        try:
            if self._bastion_config:
                return self._connect_via_bastion(timeout)
            else:
                return self._connect_direct(timeout)
        except paramiko.AuthenticationException:
            self.connect_error = {"code": self.ERR_AUTH,
                                  "message": f"认证失败: {self.username}@{self.host}，请检查用户名/密码"}
        except paramiko.ssh_exception.BadHostKeyException as e:
            self.connect_error = {"code": self.ERR_HOSTKEY,
                                  "message": f"主机密钥与 known_hosts 记录不符: {e}。"
                                             f"可能是中间人攻击或目标机重装，请人工核实后删除 "
                                             f"{KNOWN_HOSTS_FILE} 中对应条目"}
        except (socket.timeout, TimeoutError):
            self.connect_error = {"code": self.ERR_TIMEOUT,
                                  "message": f"连接超时: {self.host}:{self.port}"}
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            self.connect_error = {"code": self.ERR_UNREACHABLE,
                                  "message": f"无法连接 {self.host}:{self.port}: {e}"}
        except paramiko.SSHException as e:
            self.connect_error = {"code": self.ERR_SSH,
                                  "message": f"SSH 协议错误 {self.host}:{self.port}: {e}"}
        except Exception as e:
            self.connect_error = {"code": self.ERR_UNKNOWN,
                                  "message": f"连接失败 {self.host}:{self.port}: {e}"}
        return False

    def _connect_direct(self, timeout: int = 30) -> bool:
        client, _ = _new_client()
        client.connect(
            hostname=self.host, port=self.port,
            username=self.username, password=self.password,
            allow_agent=False, look_for_keys=False,
            timeout=timeout
        )
        self._client = client
        _save_known_hosts(client)
        self.last_activity = time.time()
        return True

    def _connect_via_bastion(self, timeout: int = 30) -> bool:
        bastion_cfg = self._bastion_config
        # 1. 连接跳板机
        bastion, _ = _new_client()
        bastion.connect(
            hostname=bastion_cfg["host"], port=bastion_cfg.get("port", 22),
            username=bastion_cfg["username"], password=bastion_cfg["password"],
            allow_agent=False, look_for_keys=False, timeout=timeout
        )
        self._bastion_client = bastion
        _save_known_hosts(bastion)

        # 2. 建立 direct-tcpip 隧道
        transport = bastion.get_transport()
        tunnel = transport.open_channel(
            "direct-tcpip",
            (self.host, self.port),
            (bastion_cfg["host"], bastion_cfg.get("port", 22))
        )

        # 3. 在隧道上连接目标机
        client, _ = _new_client()
        client.connect(
            hostname=self.host, port=self.port,
            username=self.username, password=self.password,
            sock=tunnel, allow_agent=False, look_for_keys=False,
            timeout=timeout
        )
        self._client = client
        _save_known_hosts(client)
        self.last_activity = time.time()
        return True

    # ---- 命令执行 ----

    # 非 sudo 执行时用于识别"权限不足"的 stderr 特征
    _PERMISSION_HINTS = (
        "permission denied",
        "operation not permitted",
        "must be root",
        "need root",
        "root privilege",
        "are you root",
        "only root",
        "permission denied.",
    )

    def execute(self, command: str, timeout: int = 30) -> ExecuteResult:
        """以当前用户身份直接执行命令，不附加 sudo。

        注意：timeout 到期后底层通道被关闭，但远端进程不保证被终止，
        长任务请在远端用 nohup 后台化再轮询，避免同命令重复执行。
        """
        if not self._client:
            return ExecuteResult(stderr="未建立连接", exit_code=1)

        with self._lock:
            result = self._execute_raw(command, timeout)
            self._detect_permission_denied(result)
            return result

    def execute_sudo(self, command: str, timeout: int = 30) -> ExecuteResult:
        """通过 sudo -S 提权执行命令（密码经 stdin 传入，不出现在命令行）。"""
        if not self._client:
            return ExecuteResult(stderr="未建立连接", exit_code=1)
        if not self.sudo_password:
            return ExecuteResult(
                stderr="当前凭证未配置 sudo_password，无法提权。"
                       "可通过 env update 为该凭证补充 sudo_password，或改用其他凭证（as）。",
                exit_code=1, needs_escalation=True,
                escalation_reason="缺少 sudo_password",
                escalation_options=["update_credential", "as"])

        with self._lock:
            full_cmd = f"sudo -S -p '' {command}"
            return self._execute_with_stdin(full_cmd, self.sudo_password, timeout)

    def _execute_raw(self, command: str, timeout: int = 30) -> ExecuteResult:
        """底层执行，直接调用 paramiko exec_command。"""
        t0 = time.time()
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        # 必须关闭 stdin 避免某些命令 hang
        stdin.close()
        stdout_data = stdout.read().decode("utf-8", errors="ignore")
        stderr_data = stderr.read().decode("utf-8", errors="ignore")
        exit_code = stdout.channel.recv_exit_status()
        elapsed = time.time() - t0
        self.last_activity = time.time()
        return ExecuteResult(stdout=stdout_data, stderr=stderr_data,
                             exit_code=exit_code, elapsed=elapsed)

    def _detect_permission_denied(self, result: ExecuteResult):
        """非 sudo 执行失败时，识别权限不足并给出结构化提示。"""
        if result.exit_code == 0:
            return
        stderr_lower = (result.stderr or "").lower()
        if any(hint in stderr_lower for hint in self._PERMISSION_HINTS):
            result.needs_escalation = True
            result.escalation_reason = "权限不足"
            result.escalation_options = ["escalate", "as"]

    def _execute_with_stdin(self, command: str, stdin_data: str, timeout: int = 30) -> ExecuteResult:
        """执行命令并向 stdin 写入数据（用于 sudo -S）。"""
        t0 = time.time()
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        stdin.write(stdin_data + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()

        stdout_data = stdout.read().decode("utf-8", errors="ignore")
        stderr_data = stderr.read().decode("utf-8", errors="ignore")
        exit_code = stdout.channel.recv_exit_status()
        elapsed = time.time() - t0
        self.last_activity = time.time()

        result = ExecuteResult(stdout=stdout_data, stderr=stderr_data,
                               exit_code=exit_code, elapsed=elapsed)

        # 分析 sudo 错误，给出结构化处置建议
        stderr_lower = stderr_data.lower()
        if "sudo: command not found" in stderr_lower:
            result.needs_escalation = True
            result.escalation_reason = "目标机未安装 sudo"
            result.escalation_options = ["as"]
        elif "not in the sudoers file" in stderr_lower:
            result.needs_escalation = True
            result.escalation_reason = "当前用户不在 sudoers"
            result.escalation_options = ["as"]
        elif "sorry, try again" in stderr_lower:
            result.needs_escalation = True
            result.escalation_reason = "sudo 密码错误，请检查该凭证的 sudo_password"

        return result

    # ---- 文件传输 ----

    def _ensure_sftp(self) -> Optional[paramiko.SFTPClient]:
        """惰性初始化 SFTP。"""
        if self._sftp is not None:
            return self._sftp
        if not self._client:
            return None
        try:
            self._sftp = self._client.open_sftp()
            self.last_activity = time.time()
            return self._sftp
        except Exception:
            return None

    def upload(self, local_path: str, remote_path: str) -> tuple:
        """上传文件。返回 (ok, error)。失败原因对调用方可见。"""
        sftp = self._ensure_sftp()
        if not sftp:
            return False, "无法建立 SFTP 通道"
        try:
            sftp.put(local_path, remote_path)
            self.last_activity = time.time()
            return True, ""
        except PermissionError:
            return False, (f"权限不足，无法写入 {remote_path}。"
                           f"可先上传到当前用户 home 目录，再用 escalate 执行 mv 移动到目标位置")
        except FileNotFoundError:
            return False, f"路径不存在（本地 {local_path} 或远端父目录）"
        except Exception as e:
            return False, str(e)

    def download(self, remote_path: str, local_path: str) -> tuple:
        """下载文件。返回 (ok, error)。"""
        sftp = self._ensure_sftp()
        if not sftp:
            return False, "无法建立 SFTP 通道"
        try:
            sftp.get(remote_path, local_path)
            self.last_activity = time.time()
            return True, ""
        except PermissionError:
            return False, f"权限不足，无法读取 {remote_path}"
        except FileNotFoundError:
            return False, f"远端文件不存在: {remote_path}"
        except Exception as e:
            return False, str(e)

    # ---- 连接状态 ----

    def is_alive(self) -> bool:
        """检查连接是否存活。"""
        if self._client is None:
            return False
        try:
            transport = self._client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    # ---- 心跳 ----

    def start_heartbeat(self, interval: int = 60):
        """启动心跳线程。"""
        if self._heartbeat_running:
            return
        self._heartbeat_running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(interval,),
            daemon=True
        )
        self._heartbeat_thread.start()

    def stop_heartbeat(self):
        """停止心跳线程。"""
        self._heartbeat_running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)

    def _heartbeat_loop(self, interval: int):
        while self._heartbeat_running:
            time.sleep(interval)
            if not self._heartbeat_running:
                break
            try:
                self.execute("echo __KEEPALIVE__", timeout=10)
                self.last_activity = time.time()
            except Exception:
                self._stale = True
                self._heartbeat_running = False

    # ---- 关闭 ----

    def close(self):
        """关闭连接和所有关联资源。"""
        self.stop_heartbeat()
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._bastion_client:
            try:
                self._bastion_client.close()
            except Exception:
                pass
            self._bastion_client = None
