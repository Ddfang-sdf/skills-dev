"""
IR 调用器 — 借道 ssh-connect skill 在管理节点上以 ossadm 身份调用 NCE IR (32018) 接口。

IR 是内部端口，必须从管理节点本机以 ossadm 身份调产品自带的 util.httpclient.CommonHttpClient
（处理双向 TLS + SSL_ROOT 环境变量）。本脚本构造单行 shell 命令，通过 ssh-connect skill 下发。

用法:
    python ir_caller.py <task_file.json>      # CLI 模式
    from ir_caller import call_ir             # 库模式，供 rest_run.py import

依赖: 需 ssh-connect skill 已安装在 ~/.cac/skills/ssh-connect/
"""
import json
import os
import re
import subprocess
import sys
import time

def _resolve_ssh_connect():
    """定位同级目录下的 ssh-connect skill。

    本 skill 在 <skills_root>/cloudsop-rest-call/，ssh-connect 在 <skills_root>/ssh-connect/。
    找不到直接终止并提示用户安装。
    """
    # 从当前脚本位置推导 skills 根目录
    # scripts/ir_caller.py → scripts/ → cloudsop-rest-call/ → <skills_root>/
    skills_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ssh_connect_root = os.path.join(skills_root, "ssh-connect")
    bin_exe = os.path.join(ssh_connect_root, "bin", "ssh-run.exe")
    src_py = os.path.join(ssh_connect_root, "scripts", "run.py")

    if os.path.exists(bin_exe):
        return ssh_connect_root, bin_exe
    if os.path.exists(src_py):
        return ssh_connect_root, src_py

    raise FileNotFoundError(
        f"未找到 ssh-connect skill，请先安装到 {ssh_connect_root}\n"
        f"期望位置: {bin_exe} 或 {src_py}"
    )

IR_PORT = 32018

# 输出解析正则（DOTALL：BODY 可能含多行 JSON）
STATUS_RE = re.compile(r"^STATUS:\s*(\d+)", re.MULTILINE)
BODY_RE = re.compile(r"^BODY:\s*(.*)", re.MULTILINE | re.DOTALL)


def _build_python_snippet(method, path, body_str, ir_ip, port, r21c10_plus=True):
    """构造 python -c '...' 内部的代码片段（不含外层单引号）。

    返回的字符串会被嵌入 shell 双引号内（su ossadm -c "..."），再被 python 单引号包裹。
    所以 python 字符串字面量里的双引号必须写成 \\" ——
      - 在 shell 双引号内，\\" 被 shell 解释为 \"（字面反斜杠+双引号）
      - 在 python 单引号字符串内，\" 是转义的双引号 → python 看到 "

    method:    GET/POST/PUT/DELETE/PATCH
    path:      /rest/.../path
    body_str:  JSON 字符串（POST/PUT/PATCH 用）；GET/DELETE 传空字符串
    ir_ip:     IR 服务 IP
    port:      IR 服务端口，默认 32018
    r21c10_plus: True 用 get_local_ip(), False 用 getLocalIP()
    """
    ip_fn = "common.get_local_ip()" if r21c10_plus else "common.getLocalIP()"
    # 如果 ir_ip 显式提供就用显式值，否则用 common.get_local_ip()
    # ir_ip 是 IP 字符串，嵌进 python 双引号字符串字面量，双引号要转义为 \"
    ip_expr = f'\\"{ir_ip}\\"' if ir_ip else ip_fn

    method = method.lower()
    # path 嵌进 python 双引号字符串字面量，双引号要转义为 \"
    path_escaped = path.replace('\\', '\\\\').replace('"', '\\"')
    if method in ("get", "delete"):
        call_expr = f'client.{method}(\\"{path_escaped}\\")'
    else:
        # POST/PUT/PATCH: body 是 JSON 字符串，嵌进 python 双引号字符串字面量
        # body_str 里的双引号要转义为 \"，反斜杠先转义避免连环
        body_escaped = body_str.replace('\\', '\\\\').replace('"', '\\"')
        call_expr = f'client.{method}(\\"{path_escaped}\\", \\"{body_escaped}\\")'

    return (
        f"from util import common, httpclient; "
        f"import json; "
        f'headers={{\\"Content-Type\\":\\"application/json\\"}}; '
        f"client=httpclient.CommonHttpClient({ip_expr}, {port}, True, False, headers=headers); "
        f"status, response={call_expr}; "
        f'print(\\"STATUS:\\", status); '
        f'print(\\"BODY:\\", str(response, encoding=\\"utf-8\\"))'
    )


def _build_shell_command(method, path, body_str, ir_ip=None, port=IR_PORT, r21c10_plus=True):
    """构造完整的 su ossadm -c "...python -c '...'" 单行命令。

    返回 python 内存中的字符串（会被 json.dump 写到 task 文件）。

    转义链：
      - 本函数返回的字符串在 python 内存里，双引号就是字面双引号 "
      - json.dump 写到文件时，字面双引号 " 自动转义为 \"
      - shell 读到 JSON 解析后的字符串，\" 变回字面双引号 "，是 su -c 的双引号闭合
      - python 单引号字符串内的 \" 是转义双引号 → python 看到 "

    所以本函数里：
      - su -c 的外层双引号用字面 " （json.dump 会转义）
      - python 片段内的双引号用 \" （python 内存里是字面反斜杠+双引号，json.dump 后是 \\\"，shell 看到 \"，python 看到 "）
    """
    py_snippet = _build_python_snippet(method, path, body_str, ir_ip, port, r21c10_plus)
    # su ossadm -c 内层: . engr_profile.sh && python -c '<snippet>'
    inner = f". /opt/*/manager/agent/bin/engr_profile.sh && python -c '{py_snippet}'"
    # su ossadm -c "..." —— 外层双引号用字面双引号（json.dump 自动转义为 \"）
    return f'su ossadm -c "{inner}"'


def _probe_ir_ip(target, timeout=30):
    """通过 ssh-connect 探测目标节点的 IR IP（localip）。

    Returns: IP 字符串，失败返回 None。
    """
    probe_cmd = "cat /opt/*/manager/var/agent/managerip.conf 2>/dev/null | grep -i localip"
    task = {
        "task_id": f"task_ir_probe_{int(time.time())}",
        "target": target,
        "command": probe_cmd,
        "timeout": timeout,
    }
    result = _run_ssh_connect(task)
    stdout = result.get("stdout", "")
    # 输出形如 "localip=7.222.36.7"
    m = re.search(r"localip\s*=\s*([\d.]+)", stdout)
    return m.group(1) if m else None


def _run_ssh_connect(task):
    """写 task 到 ssh-connect inbox，调 ssh-run.exe，读 result。

    task: dict，含 task_id/target/command/timeout
    Returns: result dict，含 success/exit_code/stdout/stderr
    """
    task_id = task["task_id"]
    # 解析同级目录下的 ssh-connect skill
    ssh_connect_root, ssh_connect_bin = _resolve_ssh_connect()
    ssh_connect_inbox = os.path.join(ssh_connect_root, "inbox")

    task_file = os.path.join(ssh_connect_inbox, f"{task_id}.json")

    # 写 task 文件
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False)

    # 调 ssh-run.exe
    # 注意: ssh-run.exe 在 Windows 上输出含 GBK/CP936 字节（控制台编码），
    # 不能用 encoding="utf-8"，否则 UnicodeDecodeError。用 errors="replace" 兜底。
    try:
        proc = subprocess.run(
            [ssh_connect_bin],
            capture_output=True, timeout=task.get("timeout", 120) + 30
        )
        # 优先用 result 文件（utf-8），stdout 只作 fallback
        output_bytes = proc.stdout or b""
    except Exception as e:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": f"ssh-run 调用失败: {e}"}

    # 解析 stdout 末尾的 [完整结果: outbox/result_{task_id}.json] 标记
    result_file = os.path.join(ssh_connect_root, "outbox", f"result_{task_id}.json")
    if os.path.exists(result_file):
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 没有结果文件就用 stdout（用 errors=replace 解码，避免编码崩溃）
    try:
        output_str = output_bytes.decode("utf-8", errors="replace")
    except Exception:
        output_str = str(output_bytes)
    return {"success": True, "exit_code": 0, "stdout": output_str, "stderr": ""}


def call_ir(target, method, path, body=None, port=IR_PORT, timeout=120, ir_ip=None, r21c10_plus=True):
    """调用单个 IR 接口。

    target:       ssh-connect env_name（凭证从那里读）
    method:       GET/POST/PUT/DELETE/PATCH
    path:         /rest/.../path
    body:         dict（POST/PUT/PATCH 用）；GET/DELETE 传 None
    port:         IR 服务端口，默认 32018
    timeout:      秒，默认 120（首次 python import 较慢）
    ir_ip:        显式指定 IR IP；None 则自动探测 localip
    r21c10_plus:  True 用 get_local_ip(), False 用 getLocalIP()；None 则先试新再降级

    Returns: dict {success, status, body, elapsed_s, error}
    """
    t0 = time.perf_counter()
    # body 可以是 dict（自动序列化）或 str（已是 JSON，直接用）
    if body is None:
        body_str = ""
    elif isinstance(body, str):
        body_str = body
    else:
        body_str = json.dumps(body, ensure_ascii=False)

    # 探测 IR IP
    if not ir_ip:
        ir_ip = _probe_ir_ip(target)
        if not ir_ip:
            return {"success": False, "status": -1, "body": "",
                    "elapsed_s": time.perf_counter() - t0,
                    "error": "IR_IP_NOT_FOUND: 无法从 managerip.conf 读取 localip"}

    # 版本降级处理
    if r21c10_plus is None:
        # 先试新版，失败再降级（简化：直接试新版，由调用方决定是否重试）
        r21c10_plus = True

    cmd = _build_shell_command(method, path, body_str, ir_ip, port, r21c10_plus)

    task = {
        "task_id": f"task_ir_call_{int(time.time() * 1000)}",
        "target": target,
        "command": cmd,
        "timeout": timeout,
    }

    result = _run_ssh_connect(task)
    stdout = result.get("stdout", "")
    exit_code = result.get("exit_code", -1)

    # 解析 STATUS/BODY
    status_match = STATUS_RE.search(stdout)
    body_match = BODY_RE.search(stdout)

    if not status_match:
        # 检查是否是 AttributeError (R21C10 版本问题)
        if "has no attribute 'get_local_ip'" in stdout or "getLocalIP" in stdout:
            return {"success": False, "status": -1, "body": stdout[:500],
                    "elapsed_s": time.perf_counter() - t0,
                    "error": "R21C10_VERSION_MISMATCH: 需用 getLocalIP() 旧版 API，请重试 r21c10_plus=False"}
        return {"success": False, "status": -1, "body": stdout[:1000],
                "elapsed_s": time.perf_counter() - t0,
                "error": f"PARSE_FAILED: 未找到 STATUS 行, exit_code={exit_code}, stdout={stdout[:500]}"}

    status = int(status_match.group(1))
    body = body_match.group(1) if body_match else ""

    # 尝试解析 body 为 JSON（美化输出用）
    try:
        body_parsed = json.loads(body)
        body_display = json.dumps(body_parsed, ensure_ascii=False, indent=2)
    except Exception:
        body_display = body

    return {
        "success": 200 <= status < 400,
        "status": status,
        "body": body_display,
        "elapsed_s": time.perf_counter() - t0,
        "error": "" if 200 <= status < 400 else f"HTTP_{status}",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python ir_caller.py <task_file.json>")
        print("task_file 格式: {target, method, path, body?, port?, timeout?}")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        task = json.load(f)

    result = call_ir(
        target=task["target"],
        method=task.get("method", "GET"),
        path=task["path"],
        body=task.get("body"),
        port=task.get("port", IR_PORT),
        timeout=task.get("timeout", 120),
        ir_ip=task.get("ir_ip"),
        r21c10_plus=task.get("r21c10_plus", True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
