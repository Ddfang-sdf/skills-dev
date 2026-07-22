#!/usr/bin/env python3
"""
智能SSH连接脚本 - 调试版本
支持多环境配置、命令执行、文件上传下载
"""

import paramiko
import sys
import json
import os
import re
import argparse
import time
import threading
from typing import Optional, Dict, Tuple
from io import BytesIO

ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')

def strip_ansi(text: str) -> str:
    """去除ANSI转义序列"""
    return ANSI_ESCAPE_RE.sub('', text)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'env_config.json')

DEBUG = False

def debug_log(msg):
    pass


class SudoHandler:
    """自动处理sudo密码提示"""
    
    def __init__(self, root_password: str, stdin):
        self.root_password = root_password
        self.stdin = stdin
        self.lock = threading.Lock()
    
    def handle(self, data: bytes):
        """检测输出中的密码提示，自动填写"""
        s = data.decode('utf-8', errors='ignore')
        s_lower = s.lower()
        
        password_keywords = ['password:', 'password：', '[sudo] password', '密码:', '密码：']
        for keyword in password_keywords:
            if keyword in s_lower or keyword in s:
                with self.lock:
                    try:
                        self.stdin.write((self.root_password + '\n').encode())
                        self.stdin.flush()
                    except:
                        pass
                return


class AutoSudoReader:
    """包装stdout用于自动处理sudo密码"""
    
    def __init__(self, reader, handler: SudoHandler):
        self.reader = reader
        self.handler = handler
    
    def read(self, size: int = -1) -> bytes:
        data = self.reader.read(size)
        if data:
            threading.Thread(target=self.handler.handle, args=(data,), daemon=True).start()
        return data
    
    def readline(self) -> bytes:
        data = self.reader.readline()
        if data:
            threading.Thread(target=self.handler.handle, args=(data,), daemon=True).start()
        return data


class SSHSessionWithSudo:
    def __init__(self, client, username, password, root_password, timeout=30):
        self.client = client
        self.username = username
        self.password = password
        self.root_password = root_password
        self.channel = None
        self._connect(timeout)

    def _connect(self, timeout):
        self.channel = self.client.invoke_shell()
        self.channel.setblocking(0)
        time.sleep(1)
        self._read_output()
        self.channel.send("su -\n")
        self._handle_password_prompt()
        if not self._verify_root_shell():
            raise PermissionError("Failed to switch to root shell.")

    def _read_output(self):
        output = b""
        while self.channel.recv_ready():
            try:
                output += self.channel.recv(4096)
            except:
                break
        return output.decode('utf-8', errors='ignore')

    def _handle_password_prompt(self):
        start_time = time.time()
        while time.time() - start_time < 5:
            output = self._read_output()
            if "Password:" in output:
                self.channel.send(f"{self.root_password}\n")
                return
            time.sleep(0.2)
        raise TimeoutError("No password prompt detected for su command.")

    def _verify_root_shell(self, timeout=10):
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = self._read_output()
            if "#" in output:
                return True
            if "Authentication failure" in output:
                return False
            time.sleep(0.2)
        return False

    def execute(self, command, timeout=10):
        marker = "__CMD_DONE__"
        full_command = f"{command} ; echo '{marker}'\n"
        self.channel.send(full_command)
        output = ""
        start_time = time.time()
        while time.time() - start_time < timeout:
            output += self._read_output()
            if marker in output:
                return output.split(marker)[0].strip()
            time.sleep(0.3)
        raise TimeoutError(f"Command '{command}' timed out.")

    def close(self):
        if self.channel:
            self.channel.close()
            self.channel = None


class SSHConnector:
    def __init__(self):
        self.config = self._load_config()
        self.client = None
        self.sftp = None
        self.root_password = None
        self.sudo_session = None

    def _load_config(self) -> Dict:
        """加载环境配置"""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"environments": {}}

    def get_env_config(self, env_name: str) -> Optional[Dict]:
        """获取环境配置"""
        return self.config.get("environments", {}).get(env_name)

    def find_config_by_host(self, host: str) -> Optional[Dict]:
        """通过主机名查找配置"""
        for env_config in self.config.get("environments", {}).values():
            if env_config.get("host") == host:
                return env_config
        return None

    def connect(self, host: str, port: int = 22, username: str = None,
                password: str = None, timeout: int = 30) -> Tuple[bool, str]:
        """
        建立SSH连接
        返回: (是否成功, 错误信息)
        """
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            connect_kwargs = {
                'hostname': host,
                'port': port,
                'timeout': timeout,
                'allow_agent': False,
                'look_for_keys': False
            }

            if username:
                connect_kwargs['username'] = username
            if password:
                connect_kwargs['password'] = password

            debug_log(f"正在连接到 {host}:{port}...")
            self.client.connect(**connect_kwargs)
            debug_log("连接成功")
            return True, ""

        except paramiko.AuthenticationException:
            return False, "认证失败: 用户名或密码错误"
        except paramiko.ssh_exception.NoValidConnectionsError:
            return False, f"连接失败: 无法连接到 {host}:{port}，请检查SSH服务是否启动"
        except ConnectionRefusedError:
            return False, f"连接被拒绝: {host}:{port}，SSH端口可能不是22或服务未启动"
        except TimeoutError:
            return False, f"连接超时: 无法连接到 {host}:{port}，请检查网络连接或防火墙"
        except Exception as e:
            return False, f"连接错误: {str(e)}"

    def execute_streaming(self, command: str, timeout: int = 30, use_tty: bool = False) -> Tuple[str, str, int]:
        """流式执行命令，边读边打印，避免管道阻塞
        
        注意：exec_command 使用较长的超时（3600），让 channel 保持可用。
        用户指定的 timeout 用于控制整体操作时间。
        """
        if not self.client:
            return "", "未建立连接", 1

        try:
            debug_log(f"[流式] 执行命令 (整体timeout={timeout}s, tty={use_tty}): {command[:80]}...")
            
            # 设置 UTF-8 locale 环境变量
            env_setup = 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LC_CTYPE=en_US.UTF-8; '
            
            # 如果需要 TTY，包装命令
            if use_tty:
                # 使用 script 命令强制 TTY
                escaped_cmd = command.replace('"', '\\"')
                actual_command = f'script -q -c "{env_setup}{escaped_cmd}" /dev/null'
                debug_log(f"[流式] TTY模式，实际命令: {actual_command[:100]}...")
            else:
                actual_command = env_setup + command
            
            # 使用用户指定的 timeout
            stdin, stdout, stderr = self.client.exec_command(
                actual_command,
                timeout=timeout
            )
            
            debug_log("[流式] exec_command 完成，开始流式读取...")
            
            stdout_buffer = ""
            stderr_buffer = ""
            start_time = time.time()
            
            import select
            
            import select
            import socket
            
            recv_ready_count = 0
            loop_count = 0
            stdout_done = False
            stderr_done = False
            
            # 获取 transport 和 channel id
            transport = self.client.get_transport()
            stdout_chan = stdout.channel
            stderr_chan = stderr.channel
            
            # 使用 transport 的 socket 进行 select
            sock = transport.sock
            if not isinstance(sock, socket.socket):
                debug_log("[流式] transport.sock 不是 socket 对象，使用原有逻辑")
                # Fallback to original logic
                while True:
                    loop_count += 1
                    elapsed = time.time() - start_time
                    if elapsed > timeout:
                        debug_log(f"[流式] 操作超时 ({elapsed:.1f}s > {timeout}s)")
                        break
                    if stdout.channel.recv_ready():
                        recv_ready_count += 1
                        chunk = stdout.read(4096).decode('utf-8', errors='ignore')
                        if chunk:
                            stdout_buffer += chunk
                            print(chunk, end='', flush=True)
                    else:
                        if stdout.channel.exit_status_ready():
                            stdout_done = True
                        if stderr.channel.exit_status_ready():
                            stderr_done = True
                        if stdout_done and stderr_done:
                            break
                        if loop_count % 50 == 0:
                            debug_log(f"[流式] waiting... loops={loop_count}, elapsed={elapsed:.1f}s")
                        time.sleep(0.1)
            else:
                # 使用 socket fileno 进行 select
                while True:
                    loop_count += 1
                    elapsed = time.time() - start_time
                    if elapsed > timeout:
                        debug_log(f"[流式] 操作超时 ({elapsed:.1f}s > {timeout}s)")
                        break
                    
                    try:
                        ready, _, _ = select.select([sock], [], [], 0.1)
                    except:
                        break
                    
                    if ready:
                        recv_ready_count += 1
                        if not stdout_done and stdout.channel.recv_ready():
                            chunk = stdout.read(4096).decode('utf-8', errors='ignore')
                            if chunk:
                                chunk_clean = strip_ansi(chunk)
                                stdout_buffer += chunk_clean
                                print(chunk_clean, end='', flush=True)
                        if not stderr_done and stderr.channel.recv_ready():
                            chunk = stderr.read(4096).decode('utf-8', errors='ignore')
                            if chunk:
                                chunk_clean = strip_ansi(chunk)
                                stderr_buffer += chunk_clean
                                print(chunk_clean, end='', file=sys.stderr, flush=True)
                    
                    if not stdout_done and stdout.channel.exit_status_ready():
                        stdout_done = True
                    if not stderr_done and stderr.channel.exit_status_ready():
                        stderr_done = True
                    
                    if stdout_done and stderr_done:
                        break
                    
                    if loop_count % 50 == 0:
                        debug_log(f"[流式] waiting... loops={loop_count}, stdout_done={stdout_done}, stderr_done={stderr_done}, elapsed={elapsed:.1f}s")
            
            exit_code = stdout.channel.recv_exit_status()
            debug_log(f"[流式] 完成, exit_code={exit_code}, stdout长度={len(stdout_buffer)}, stderr长度={len(stderr_buffer)}, 耗时={time.time()-start_time:.1f}s")
            
            return stdout_buffer, stderr_buffer, exit_code
        except Exception as e:
            debug_log(f"[流式] execute 异常: {type(e).__name__}: {e}")
            return "", str(e), 1

    def execute(self, command: str, timeout: int = 120) -> Tuple[str, str, int]:
        """执行命令"""
        if not self.client:
            return "", "未建立连接", 1

        try:
            debug_log(f"执行命令 (timeout={timeout}s): {command[:80]}...")
            
            # 设置 UTF-8 locale 环境变量
            env_setup = 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LC_CTYPE=en_US.UTF-8; '
            full_command = env_setup + command
            
            stdin, stdout, stderr = self.client.exec_command(
                full_command,
                timeout=timeout
            )
            
            debug_log("exec_command 完成，等待 stdout.read()...")
            
            stdout_data = stdout.read()
            debug_log(f"stdout.read() 完成, 长度={len(stdout_data)}")
            
            stderr_data = stderr.read()
            debug_log(f"stderr.read() 完成, 长度={len(stderr_data)}")
            
            debug_log("等待 recv_exit_status()...")
            exit_code = stdout.channel.recv_exit_status()
            debug_log(f"recv_exit_status 完成, exit_code={exit_code}")
            
            stdout_decoded = stdout_data.decode('utf-8', errors='ignore')
            stderr_decoded = stderr_data.decode('utf-8', errors='ignore')
            
            return strip_ansi(stdout_decoded), strip_ansi(stderr_decoded), exit_code
        except Exception as e:
            debug_log(f"execute 异常: {type(e).__name__}: {e}")
            return "", str(e), 1

    def execute_with_sudo(self, command: str, timeout: int = 120) -> Tuple[str, str, int]:
        """执行命令，自动处理sudo密码提示"""
        if not self.client:
            return "", "未建立连接", 1

        if not self.root_password:
            return self.execute(command, timeout)

        try:
            if not self.sudo_session:
                self.sudo_session = SSHSessionWithSudo(
                    self.client,
                    self.client.get_transport().getpeername()[0],
                    None,
                    self.root_password,
                    timeout=30
                )

            output = self.sudo_session.execute(command, timeout=timeout)
            return output, "", 0

        except PermissionError as e:
            return "", str(e), 1
        except TimeoutError as e:
            return "", str(e), 1
        except Exception as e:
            return "", str(e), 1

    def _sanitize_remote_path(self, path: str) -> str:
        """处理Git Bash路径转换问题：/root/... -> D:/emviroment/Git/root/..."""
        if not path:
            return path
        path_str = str(path)
        if ('emviroment' in path_str.lower() or 'environment' in path_str.lower()) and 'git' in path_str.lower():
            parts = re.split(r'[/\\]', path_str)
            new_parts = []
            skip_next = False
            for i, part in enumerate(parts):
                if skip_next:
                    skip_next = False
                    continue
                if part.lower() in ('emviroment', 'environment'):
                    if i + 1 < len(parts) and parts[i + 1].lower() == 'git':
                        skip_next = True
                        continue
                new_parts.append(part)
            if new_parts and new_parts[0].endswith(':'):
                new_parts.pop(0)
            path_str = '/'.join(new_parts)
            if path_str.startswith('root') or path_str.startswith('Root'):
                path_str = '/' + path_str
        return path_str

    def upload(self, local_path: str, remote_path: str) -> Tuple[bool, str]:
        """上传文件"""
        if not self.client:
            return False, "未建立连接"

        local_path = os.path.normpath(local_path)

        if not os.path.exists(local_path):
            return False, f"本地文件不存在: {local_path}"

        try:
            if not self.sftp:
                self.sftp = self.client.open_sftp()
            self.sftp.put(local_path, remote_path)
            return True, f"上传成功: {local_path} -> {remote_path}"
        except FileNotFoundError:
            return False, f"上传失败: sftp.put FileNotFoundError，local={local_path}, remote={remote_path}"
        except Exception as e:
            return False, f"上传失败: {str(e)}"

    def download(self, remote_path: str, local_path: str) -> Tuple[bool, str]:
        """下载文件"""
        if not self.client:
            return False, "未建立连接"

        local_path = os.path.normpath(local_path)

        try:
            if not self.sftp:
                self.sftp = self.client.open_sftp()
            self.sftp.get(remote_path, local_path)
            return True, f"下载成功: {remote_path} -> {local_path}"
        except FileNotFoundError:
            return False, f"下载失败: sftp.get FileNotFoundError，remote={remote_path}, local={local_path}"
        except Exception as e:
            return False, f"下载失败: {str(e)}"

    def list_dir(self, remote_path: str) -> Tuple[bool, str]:
        """列出远程目录"""
        if not self.client:
            return False, "未建立连接"

        try:
            if not self.sftp:
                self.sftp = self.client.open_sftp()
            files = self.sftp.listdir(remote_path)
            return True, "\n".join(files)
        except Exception as e:
            return False, f"列出目录失败: {str(e)}"

    def close(self):
        """关闭连接"""
        if self.sftp:
            self.sftp.close()
            self.sftp = None
        if self.client:
            self.client.close()
            self.client = None


def parse_host_user(host_str: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    解析 hostStr
    支持格式:
    - 192.168.1.100
    - user@192.168.1.100
    - user/password@192.168.1.100
    """
    host = host_str
    username = None
    password = None

    if '@' in host_str:
        user_part, host = host_str.split('@', 1)
        if '/' in user_part:
            username, password = user_part.split('/', 1)
        else:
            username = user_part

    return host, username, password


def main():
    parser = argparse.ArgumentParser(description='智能SSH连接')
    parser.add_argument('--json', type=str, required=True, help='JSON格式的请求体')

    args = parser.parse_args()

    try:
        req = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}", file=sys.stderr)
        sys.exit(1)

    target = req.get('target')
    if not target:
        print("错误: JSON 中缺少 target 字段", file=sys.stderr)
        sys.exit(1)

    timeout = req.get('timeout', 30)
    if timeout < 1:
        timeout = 1
    elif timeout > 3600:
        timeout = 3600

    command = req.get('command')
    upload = req.get('upload')
    download = req.get('download')

    ops = sum([1 for x in [command, upload, download] if x])
    if ops == 0:
        print("错误: 必须指定 command、upload 或 download 之一", file=sys.stderr)
        sys.exit(1)
    if ops > 1:
        print("错误: command、upload、download 只能指定一个", file=sys.stderr)
        sys.exit(1)

    connector = SSHConnector()

    # 1. 尝试直接解析
    host, cli_username, cli_password = parse_host_user(target)

    # 2. 如果不是IP格式，尝试作为环境名查找
    if not cli_username:
        env_config = connector.get_env_config(target)
        if env_config:
            host = env_config.get("host", host)
            cli_username = env_config.get("username")
            cli_password = env_config.get("password")

    # 3. 如果host不是IP，尝试从配置中查找
    if not cli_username:
        host_config = connector.find_config_by_host(target)
        if host_config:
            host = host_config.get("host", host)
            cli_username = host_config.get("username")
            cli_password = host_config.get("password")

    # 4. 检查是否有足够信息
    if not cli_username or not cli_password:
        print("错误: 缺少登录凭证", file=sys.stderr)
        print(f"请提供用户名和密码，或在 env_config.json 中配置 {target} 环境", file=sys.stderr)
        sys.exit(1)

    print(f"正在连接到 {host}...", file=sys.stderr)

    # 连接
    success, error_msg = connector.connect(
        host=host,
        username=cli_username,
        password=cli_password
    )

    if not success:
        print(f"连接失败: {error_msg}", file=sys.stderr)
        sys.exit(1)

    print(f"已连接", file=sys.stderr)

    # 设置root_password（如果配置中有）
    if not cli_username:
        env_config = connector.get_env_config(target)
        if env_config:
            connector.root_password = env_config.get("root_password")
    if not connector.root_password:
        host_config = connector.find_config_by_host(target)
        if host_config:
            connector.root_password = host_config.get("root_password")

    # 执行操作
    if upload:
        local_path = upload.get('local')
        remote_path = upload.get('remote')
        success, msg = connector.upload(local_path, remote_path)
        print(msg)
        connector.close()
        sys.exit(0 if success else 1)

    if download:
        remote_path = download.get('remote')
        local_path = download.get('local')
        success, msg = connector.download(remote_path, local_path)
        print(msg)
        connector.close()
        sys.exit(0 if success else 1)

    if command:
        cmd_prefix = command.strip().split()[0] if command.strip() else ''
        auto_tty = cmd_prefix in ('mvn', 'npm', 'gradle', 'make', 'configure')
        stdout, stderr, exit_code = connector.execute_streaming(command, timeout=timeout, use_tty=auto_tty)
        if stderr:
            print(stderr, file=sys.stderr)
        connector.close()
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
