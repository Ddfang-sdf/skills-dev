#!/usr/bin/env python3
"""
SSH连接客户端
支持远程命令执行、文件操作等
"""

import paramiko
import sys
import json
import argparse
from typing import Optional, Tuple


class SSHClient:
    def __init__(self, host: str, port: int = 22, username: str = None, password: str = None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.sftp = None
        
    def connect(self, timeout: int = 30) -> bool:
        """建立SSH连接"""
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            connect_kwargs = {
                'hostname': self.host,
                'port': self.port,
                'timeout': timeout,
                'allow_agent': False,
                'look_for_keys': False
            }
            
            if self.username:
                connect_kwargs['username'] = self.username
            if self.password:
                connect_kwargs['password'] = self.password
                
            self.client.connect(**connect_kwargs)
            return True
        except Exception as e:
            print(f"连接失败: {str(e)}", file=sys.stderr)
            return False
    
    def execute(self, command: str, timeout: int = 120, get_pty: bool = False) -> Tuple[str, str, int]:
        """
        执行远程命令
        返回: (stdout, stderr, exit_code)
        """
        if not self.client:
            return "", "未建立连接", 1
            
        try:
            stdin, stdout, stderr = self.client.exec_command(
                command, 
                timeout=timeout,
                get_pty=get_pty
            )
            
            stdout_data = stdout.read().decode('utf-8', errors='ignore')
            stderr_data = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()
            
            return stdout_data, stderr_data, exit_code
        except Exception as e:
            return "", str(e), 1
    
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """上传文件"""
        if not self.client:
            return False
            
        try:
            if not self.sftp:
                self.sftp = self.client.open_sftp()
                
            self.sftp.put(local_path, remote_path)
            return True
        except Exception as e:
            print(f"上传失败: {str(e)}", file=sys.stderr)
            return False
    
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """下载文件"""
        if not self.client:
            return False
            
        try:
            if not self.sftp:
                self.sftp = self.client.open_sftp()
                
            self.sftp.get(remote_path, local_path)
            return True
        except Exception as e:
            print(f"下载失败: {str(e)}", file=sys.stderr)
            return False
    
    def close(self):
        """关闭连接"""
        if self.sftp:
            self.sftp.close()
            self.sftp = None
        if self.client:
            self.client.close()
            self.client = None


def parse_connection(conn_str: str) -> Tuple[str, int, Optional[str], Optional[str]]:
    """
    解析连接字符串
    格式: username/password@host:port 或 host:port 或 host
    返回: (host, port, username, password)
    """
    host = conn_str
    port = 22
    username: Optional[str] = None
    password: Optional[str] = None
    
    # 解析用户名密码和主机
    if '@' in conn_str:
        user_pass, host_part = conn_str.split('@', 1)
        if '/' in user_pass:
            username, password = user_pass.split('/', 1)
        else:
            username = user_pass
        host = host_part
    
    # 解析端口
    if ':' in host:
        host, port_str = host.split(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            pass
    
    return host, port, username, password


def main():
    parser = argparse.ArgumentParser(description='SSH远程连接客户端')
    parser.add_argument('host', help='主机地址 (IP或主机名)')
    parser.add_argument('-p', '--port', type=int, default=22, help='SSH端口 (默认: 22)')
    parser.add_argument('-u', '--username', help='用户名')
    parser.add_argument('-P', '--password', help='密码')
    parser.add_argument('-c', '--command', help='要执行的命令')
    parser.add_argument('-t', '--timeout', type=int, default=120, help='命令超时时间(秒)')
    parser.add_argument('--json', action='store_true', help='输出JSON格式结果')
    
    args = parser.parse_args()
    
    # 建立连接
    client = SSHClient(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password
    )
    
    if not client.connect():
        if args.json:
            print(json.dumps({'success': False, 'error': '连接失败'}))
        sys.exit(1)
    
    # 执行命令
    if args.command:
        stdout, stderr, exit_code = client.execute(args.command, timeout=args.timeout)
        
        if args.json:
            print(json.dumps({
                'success': exit_code == 0,
                'stdout': stdout,
                'stderr': stderr,
                'exit_code': exit_code
            }, ensure_ascii=False))
        else:
            if stdout:
                print(stdout)
            if stderr:
                print(stderr, file=sys.stderr)
        
        client.close()
        sys.exit(exit_code)
    else:
        # 交互模式
        print(f"已连接到 {args.host}", file=sys.stderr)
        print("输入命令执行（输入 'exit' 退出）", file=sys.stderr)
        
        while True:
            try:
                cmd = input("\n$ ")
                if cmd.lower() in ('exit', 'quit'):
                    break
                    
                stdout, stderr, exit_code = client.execute(cmd)
                print(stdout)
                if stderr:
                    print(stderr, file=sys.stderr)
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\n退出")
                break
        
        client.close()


if __name__ == '__main__':
    main()
