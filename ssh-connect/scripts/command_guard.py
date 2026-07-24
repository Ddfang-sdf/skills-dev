#!/usr/bin/env python3
"""命令与传输路径安全检查模块

【定位说明】本模块是"防误操作"的兜底，不是安全边界。
基于字符串模式匹配，无法抵御刻意构造的绕过手段（混淆、编码、
拼接、写入脚本后间接执行等）。SKILL.md 已明确禁止 LLM 以任何
形式规避检查，并禁止在首次执行时携带 force=true。
"""

import re
from dataclasses import dataclass
from typing import Optional


# ==================== 命令规则 ====================
# 预编译正则列表 (pattern, reason)
# BLOCK：毁灭性操作，任何情况下都不执行（force=true 也不放行）
_BLOCK_PATTERNS = [
    # rm 递归+强制删除 根目录 / /* / ~（兼容 -rf、-fr、-r -f、--recursive 等写法）
    (re.compile(r"\brm\s+(?:-[a-zA-Z-]+\s+)*-[a-zA-Z]*(?:[rR][a-zA-Z]*[fF]|[fF][a-zA-Z]*[rR])[a-zA-Z]*\s+(?:--\s+)?(?:/|/\*|~)(?:\s|$)"),
     "禁止递归强制删除根目录或 home 目录"),
    (re.compile(r"\brm\s+(?:-[a-zA-Z-]+\s+)*-[a-zA-Z]*(?:[rR][a-zA-Z]*[fF]|[fF][a-zA-Z]*[rR])[a-zA-Z]*\s+(?:--\s+)?\.\s*$"),
     "禁止递归强制删除当前目录"),
    (re.compile(r"\brm\s+--recursive\s+--force\s+(?:--\s+)?(?:/|/\*|~)(?:\s|$)"),
     "禁止递归强制删除根目录或 home 目录"),
    # 写块设备
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/(?:sd|hd|vd|xvd|nvme|mmcblk)"),
     "禁止直接写入块设备"),
    (re.compile(r">{1,2}\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk)"),
     "禁止覆盖块设备"),
    (re.compile(r"\b(?:shred|wipe)\b[^|;&]*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk)"),
     "禁止粉碎块设备"),
    # 格式化 / 抹除分区表
    (re.compile(r"\b(?:mkfs(?:\.\w+)?|mke2fs|wipefs)\b"),
     "禁止格式化或抹除文件系统"),
    # fork 炸弹
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;?\s*:"),
     "禁止执行 fork 炸弹"),
    # 递归破坏根目录权限/属主
    (re.compile(r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+777\s+/(?:\s|$)"),
     "禁止递归设置根目录为全局可写"),
    (re.compile(r"\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\s+\S+\s+/(?:\s|$)"),
     "禁止递归改变根目录所有者"),
]

# WARN：高风险操作，默认拦截，用户确认后 force=true 放行
_WARN_PATTERNS = [
    (re.compile(r"\brm\s+(?:-[a-zA-Z-]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*(?:\s|$)"),
     "递归删除"),
    (re.compile(r"\bsed\s+-i\b"),
     "原地修改文件"),
    (re.compile(r"\b(?:kill|killall|pkill)\s+-9\b"),
     "强制杀死进程"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b|\bsystemctl\s+(?:poweroff|reboot|halt)\b"),
     "系统关机/重启"),
    (re.compile(r"\biptables\s+-[ADIRF]\b|\bnft\s+(?:add|delete|flush)\b|\bufw\s+(?:allow|deny|delete|disable)\b|\bfirewall-cmd\s+--(?:add|remove|change)"),
     "修改防火墙规则"),
    (re.compile(r"\bsystemctl\s+(?:stop|disable|mask)\b|\bservice\s+\S+\s+stop\b"),
     "停止/禁用系统服务"),
    (re.compile(r"\bchmod\s+(?:-[a-zA-Z]*R[a-zA-Z]*\s+)?777\b"),
     "设置全局可写权限"),
    (re.compile(r"\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\b"),
     "递归改变文件所有者"),
    (re.compile(r"\b(?:useradd|userdel|usermod|groupadd|groupdel|passwd|visudo)\b"),
     "修改用户/权限配置"),
    (re.compile(r">{1,2}\s*/etc/sudoers(?:\.d/|\s|$)"),
     "修改 sudoers 配置"),
    (re.compile(r"\b(?:fdisk|sfdisk|parted)\b"),
     "磁盘分区操作"),
    (re.compile(r">{1,2}\s*/(?:etc|boot|bin|sbin|lib|lib64|usr)(?:/|\s|$)"),
     "写入系统目录"),
    (re.compile(r"\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|python\d*(?:\.\d+)?|perl|ruby)\b"),
     "管道内容直接交给解释器执行（如 curl ... | bash）"),
    (re.compile(r"\bbase64\s+(?:-d\b|--decode\b)"),
     "base64 解码（可能用于混淆命令，需人工确认意图）"),
    (re.compile(r"\bfind\b[^|;&]*\s-delete\b"),
     "find -delete 批量删除"),
    (re.compile(r"\bcrontab\s+-[eri]\b"),
     "修改/清空定时任务"),
    (re.compile(r"(?:/etc/shadow|/etc/gshadow|/etc/sudoers\b|authorized_keys|id_rsa|id_ed25519|id_ecdsa)"),
     "访问敏感凭据文件"),
    (re.compile(r"\bsshd?_config\b"),
     "修改 SSH 服务配置"),
]

# ==================== 传输路径规则 ====================
# 上传/下载的远端路径命中以下位置时，按 WARN 处理（需用户确认 + force=true）
_SENSITIVE_REMOTE_PATHS = [
    (re.compile(r"^/etc/(?:shadow|gshadow|sudoers)(?:$|\.|/)"),
     "系统凭据文件"),
    (re.compile(r"^/root(?:/|$)"),
     "root 用户目录"),
    (re.compile(r"/\.ssh/(?:authorized_keys|id_rsa|id_ed25519|id_ecdsa|known_hosts)$"),
     "SSH 密钥/授权文件"),
    (re.compile(r"^/(?:boot|bin|sbin|lib|lib64|usr/bin|usr/sbin|usr/lib|usr/lib64|usr/local/bin)(?:/|$)"),
     "系统程序目录"),
]

# rm 递归强制删除时，命中这些目标即 BLOCK（与选项写法无关）
_RM_ROOT_TARGETS = {"/", "/*", "~", "~/", "."}


@dataclass
class CheckResult:
    level: str          # "block" | "warn" | "allow"
    blocked: bool       # 是否被拦截
    reason: Optional[str] = None    # 拦截原因
    matched: Optional[str] = None   # 匹配到的正则模式


class CommandGuard:
    """命令与传输路径检查器。使用内置规则进行正则匹配。"""

    def check(self, command: str) -> CheckResult:
        """检查命令，返回最高级别的匹配结果。"""
        # 空命令直接拒绝
        if not command or command.strip() == "":
            return CheckResult(level="block", blocked=True, reason="空命令")

        # 先做 rm 专项解析（覆盖 -r -f 分写等正则难覆盖的写法）
        rm_block = self._check_rm_root(command)
        if rm_block:
            return rm_block

        # 先遍历 BLOCK 规则
        for pattern, reason in _BLOCK_PATTERNS:
            if pattern.search(command):
                return CheckResult(level="block", blocked=True, reason=reason, matched=pattern.pattern)

        # 再遍历 WARN 规则
        for pattern, reason in _WARN_PATTERNS:
            if pattern.search(command):
                return CheckResult(level="warn", blocked=True, reason=reason, matched=pattern.pattern)

        # 均未命中
        return CheckResult(level="allow", blocked=False)

    @staticmethod
    def _check_rm_root(command: str) -> Optional[CheckResult]:
        """解析 rm 命令：选项中同时含递归(r/R)与强制(f)且目标为根/home/当前目录时 BLOCK。

        用解析而非纯正则，覆盖 `rm -r -f /`、`rm -v -r --force /` 等写法。
        """
        tokens = command.strip().split()
        # 跳过前导 sudo/env 赋值等常见前缀
        while tokens and (tokens[0] == "sudo" or "=" in tokens[0]):
            tokens.pop(0)
        if not tokens or tokens[0] != "rm":
            return None
        flags = ""
        targets = []
        for tok in tokens[1:]:
            if tok == "--":
                continue
            if tok.startswith("--"):
                if "recursive" in tok:
                    flags += "r"
                if "force" in tok:
                    flags += "f"
                continue
            if tok.startswith("-") and len(tok) > 1:
                flags += tok[1:]
            else:
                targets.append(tok.rstrip("/") if tok != "/" else tok)
        if "f" not in flags or ("r" not in flags and "R" not in flags):
            return None
        for t in targets:
            if t in _RM_ROOT_TARGETS:
                return CheckResult(level="block", blocked=True,
                                   reason="禁止递归强制删除根目录或 home 目录",
                                   matched="rm-parsed")
        return None

    def check_transfer(self, remote_path: str) -> CheckResult:
        """检查上传/下载的远端路径是否涉及敏感位置。"""
        path = (remote_path or "").strip()
        if not path:
            return CheckResult(level="block", blocked=True, reason="远端路径为空")
        for pattern, label in _SENSITIVE_REMOTE_PATHS:
            if pattern.search(path):
                return CheckResult(level="warn", blocked=True,
                                   reason=f"远端路径涉及敏感位置（{label}）: {path}",
                                   matched=pattern.pattern)
        return CheckResult(level="allow", blocked=False)

    @staticmethod
    def can_execute(level: str, force: bool) -> bool:
        """判定给定级别是否可执行。

        force=true 时仅 block 禁止，warn 和 allow 放行
        force=false 时仅 allow 放行
        """
        if level == "allow":
            return True
        if level == "warn":
            return force is True
        if level == "block":
            return False
        return False
