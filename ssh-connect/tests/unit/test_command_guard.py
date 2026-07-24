#!/usr/bin/env python3
"""P1: command_guard.py 单元测试

测试 CommandGuard.check() 和 CommandGuard.can_execute() 两个公开方法。
纯函数测试，无需 mock。
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))

import pytest
from command_guard import CommandGuard, CheckResult


class TestCommandGuardCheck:
    """CommandGuard.check() 测试"""

    def setup_method(self):
        self.guard = CommandGuard()

    # --- U-P1-01: ALLOW 级 ---
    def test_allow_safe_read_commands(self):
        """安全的只读命令不应被拦截"""
        cases = ["ls -la", "cat /etc/hosts", "ps aux", "df -h", "echo hello"]
        for cmd in cases:
            result = self.guard.check(cmd)
            assert result.level == "allow", f"命令 '{cmd}' 应被放行，实际 level={result.level}"
            assert result.blocked == False

    # --- U-P1-02: WARN 级 — rm ---
    def test_warn_rm_recursive(self):
        """rm -rf /tmp/logs 应命中 WARN"""
        result = self.guard.check("rm -rf /tmp/logs")
        assert result.level == "warn"
        assert result.blocked == True
        assert "递归删除" in result.reason

    # --- U-P1-03: BLOCK 级 — rm -rf / ---
    def test_block_rm_rf_root(self):
        """rm -rf / 应命中 BLOCK"""
        result = self.guard.check("rm -rf /")
        assert result.level == "block"
        assert result.blocked == True

    # --- U-P1-05: 空命令 ---
    def test_block_empty_command(self):
        """空字符串应被 BLOCK"""
        r1 = self.guard.check("")
        assert r1.level == "block"
        assert "空命令" in r1.reason

        r2 = self.guard.check("   ")
        assert r2.level == "block"

    # --- U-P1-06: BLOCK 优先于 WARN ---
    def test_block_priority_over_warn(self):
        """BLOCK 规则优先 WARN 匹配"""
        # dd 写块设备命中 block
        r1 = self.guard.check("dd if=/dev/zero of=/dev/sda")
        assert r1.level == "block"

        # rm -rf 普通路径仅命中 warn
        r2 = self.guard.check("rm -rf /tmp/logs")
        assert r2.level == "warn"

    # --- U-P1-07: WARN 级 — sed -i, kill -9, shutdown ---
    def test_warn_sed_kill_shutdown(self):
        """sed -i, kill -9, shutdown 命中 WARN"""
        r1 = self.guard.check("sed -i 's/a/b/' file.txt")
        assert r1.level == "warn"
        assert r1.blocked == True

        r2 = self.guard.check("kill -9 1234")
        assert r2.level == "warn"
        assert r2.blocked == True

        r3 = self.guard.check("shutdown -h now")
        assert r3.level == "warn"
        assert r3.blocked == True

    # --- U-P1-08: BLOCK 级 — mkfs, 覆盖块设备 ---
    def test_block_mkfs_truncate_device(self):
        """mkfs, > /dev/sda 命中 BLOCK"""
        r1 = self.guard.check("mkfs.ext4 /dev/sda1")
        assert r1.level == "block"

        r2 = self.guard.check("> /dev/sda")
        assert r2.level == "block"


class TestCommandGuardCanExecute:
    """CommandGuard.can_execute() 测试"""

    # --- U-P1-04: force 行为 ---
    def test_force_warn_becomes_executable(self):
        """force=true 仅放行 WARN，BLOCK 始终拒绝，ALLOW 始终可执行"""
        # warn + force → 可执行
        assert CommandGuard.can_execute("warn", True) == True

        # block + force → 不可执行
        assert CommandGuard.can_execute("block", True) == False

        # allow + no force → 可执行
        assert CommandGuard.can_execute("allow", False) == True

    def test_force_false_only_allow_passes(self):
        """force=false 时仅 allow 可执行"""
        assert CommandGuard.can_execute("allow", False) == True
        assert CommandGuard.can_execute("warn", False) == False
        assert CommandGuard.can_execute("block", False) == False
