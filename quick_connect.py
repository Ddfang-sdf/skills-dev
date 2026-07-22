#!/usr/bin/env python3
"""
快速SSH连接测试脚本
用法: python quick_connect.py <host> <username> <password> [command]
"""

import sys
import os

# 添加scripts目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_client import SSHClient


def main():
    if len(sys.argv) < 2:
        print("用法: python quick_connect.py <host> [username] [password] [command]")
        print("示例: python quick_connect.py 7.189.26.215 root Changeme_456 'ps aux | grep java'")
        sys.exit(1)
    
    host = sys.argv[1]
    username = sys.argv[2] if len(sys.argv) > 2 else None
    password = sys.argv[3] if len(sys.argv) > 3 else None
    command = sys.argv[4] if len(sys.argv) > 4 else None
    
    client = SSHClient(host=host, username=username, password=password)
    
    print(f"连接到 {host}...", file=sys.stderr)
    
    if not client.connect():
        print("连接失败", file=sys.stderr)
        sys.exit(1)
    
    print(f"已连接，执行命令...", file=sys.stderr)
    
    if command:
        stdout, stderr, exit_code = client.execute(command)
        print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        client.close()
        sys.exit(exit_code)
    else:
        print("交互模式 (输入 'exit' 退出)", file=sys.stderr)
        while True:
            try:
                cmd = input("\n$ ")
                if cmd.lower() in ('exit', 'quit'):
                    break
                stdout, stderr, exit_code = client.execute(cmd)
                print(stdout)
                if stderr:
                    print(stderr, file=sys.stderr)
            except (EOFError, KeyboardInterrupt):
                break
        client.close()


if __name__ == '__main__':
    main()
