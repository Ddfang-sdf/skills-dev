"""
CloudSOP REST 调用主调度脚本 — 读 inbox/task_*.json，按 mode (er/ir) 分流到对应调用器。

用法:
    python rest_run.py                     # 扫描 inbox/ 所有 task_*.json 并执行
    python rest_run.py <task_file.json>    # 只执行指定 task 文件
    python rest_run.py --help              # 帮助

task 文件格式:
    ER 模式:
        {
          "task_id": "task_1",
          "mode": "er",
          "target": "7.222.36.7",
          "user": "admin",
          "pwd": "Changeme_123",
          "calls": [
            {"method": "POST", "path": "/rest/.../api", "body_file": "body_1.json"},
            {"method": "GET",  "path": "/rest/.../api"}
          ]
        }
    IR 模式:
        {
          "task_id": "task_2",
          "mode": "ir",
          "target": "7.222.36.7",
          "calls": [
            {"method": "GET",  "path": "/rest/.../healthcheck"},
            {"method": "POST", "path": "/rest/.../api", "body": {"k": "v"}}
          ]
        }

依赖:
    - ER 模式: requests, urllib3 (本机直连 HTTPS)
    - IR 模式: ssh-connect skill 已安装 (~/.cac/skills/ssh-connect/)
"""
import argparse
import glob
import json
import os
import sys
import time

# 让 er_login / ir_caller 可被 import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from er_login import login as er_login_fn, ER_PORT, is_authorized


def print_help():
    print("""
CloudSOP REST 调用主调度脚本

用法:
    python rest_run.py                     扫描 inbox/ 所有 task_*.json 并执行
    python rest_run.py <task_file.json>    只执行指定 task 文件
    python rest_run.py --help              显示本帮助

task 文件位置: <SKILL_ROOT>/inbox/task_{id}.json
结果文件位置: <SKILL_ROOT>/outbox/result_{id}.json

task 文件格式见脚本头部注释。

输出:
    每个调用按 Postman 风格打印: method url / status time size / body
    末尾汇总表: [OK/FAIL] status path
    完整结果 JSON 写到 outbox/result_{task_id}.json
""")


def load_body(call, inbox_dir):
    """从 call 里取 body。优先 body_file，其次 body 字段。返回字符串。"""
    if "body_file" in call:
        body_path = call["body_file"]
        if not os.path.isabs(body_path):
            body_path = os.path.join(inbox_dir, body_path)
        with open(body_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    if "body" in call:
        b = call["body"]
        return b if isinstance(b, str) else json.dumps(b, ensure_ascii=False)
    return ""


def print_call_header(mode, method, target, path):
    """Postman 风格的调用头"""
    port = ER_PORT if mode == "er" else 32018
    print(f"\n{mode.upper()} {method.upper()} https://{target}:{port}{path}")


def print_call_result(status, elapsed_s, size, body):
    """Postman 风格的调用结果"""
    time_str = f"{elapsed_s*1000:.0f} ms" if elapsed_s < 1 else f"{elapsed_s:.2f} s"
    print(f"status={status}  time={time_str}  size={size} bytes")
    if body:
        # 尝试美化 JSON
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        except Exception:
            print(body if isinstance(body, str) else str(body))
    else:
        print("(empty body)")


def run_er_task(task, inbox_dir):
    """执行 ER task: 登录一次，批量调 calls，复用同一 cookie/roarand"""
    target = task["target"]
    user = task.get("user", "admin")
    pwd = task.get("pwd", "Changeme_123")
    calls = task.get("calls", [])

    print(f"\n=== ER 模式: target={target}, {len(calls)} 个调用 ===")

    # 登录
    print("--- 登录中 ---")
    try:
        client, headers, bsp, csrf = er_login_fn(target, user, pwd, verbose=True)
    except Exception as e:
        print(f"!!! 登录失败: {e}")
        return {"success": False, "error": str(e), "results": []}
    print("--- 登录完成 ---")

    results = []
    for i, call in enumerate(calls, 1):
        method = call.get("method", "GET")
        path = call["path"]
        body_str = load_body(call, inbox_dir)

        print(f"\n--- [{i}/{len(calls)}] ---")
        print_call_header("er", method, target, path)
        if body_str:
            preview = body_str[:200] + ("..." if len(body_str) > 200 else "")
            print(f">>> body: {preview}")

        # 发请求
        t0 = time.perf_counter()
        try:
            url = f"https://{target}:{ER_PORT}{path}"
            r = client.request(
                method.upper(), url,
                data=body_str.encode("utf-8") if body_str else None,
                headers=headers, verify=False
            )
            elapsed_s = time.perf_counter() - t0
            size = len(r.content)

            # 401 自动重登一次
            if is_authorized(r.status_code, r.text):
                print(">>> 401, 自动重登...")
                try:
                    client, headers, _, _ = er_login_fn(target, user, pwd, verbose=False)
                    r = client.request(
                        method.upper(), url,
                        data=body_str.encode("utf-8") if body_str else None,
                        headers=headers, verify=False
                    )
                    elapsed_s = time.perf_counter() - t0
                    size = len(r.content)
                except Exception as re_err:
                    print(f"!!! 重登失败: {re_err}")

            print_call_result(r.status_code, elapsed_s, size, r.text)
            results.append({
                "path": path, "method": method.upper(),
                "status": r.status_code, "ok": 200 <= r.status_code < 300,
                "body": r.text, "elapsed_s": elapsed_s, "size": size,
            })
        except Exception as e:
            elapsed_s = time.perf_counter() - t0
            print(f"!!! 调用异常: {e}")
            results.append({
                "path": path, "method": method.upper(),
                "status": -1, "ok": False, "body": "",
                "elapsed_s": elapsed_s, "size": 0, "error": str(e),
            })

    return {"success": all(r["ok"] for r in results), "results": results}


def run_ir_task(task, inbox_dir):
    """执行 IR task: 通过 ssh-connect 逐个调用"""
    from ir_caller import call_ir

    target = task["target"]
    calls = task.get("calls", [])
    port = task.get("port", 32018)
    timeout = task.get("timeout", 120)

    print(f"\n=== IR 模式: target={target}, {len(calls)} 个调用, port={port} ===")

    results = []
    for i, call in enumerate(calls, 1):
        method = call.get("method", "GET")
        path = call["path"]
        body = call.get("body")  # dict 或 None
        if "body_file" in call:
            body_str = load_body(call, inbox_dir)
            try:
                body = json.loads(body_str)
            except Exception:
                body = body_str

        print(f"\n--- [{i}/{len(calls)}] ---")
        print_call_header("ir", method, target, path)
        if body:
            preview = (json.dumps(body, ensure_ascii=False)[:200] + "...") if len(json.dumps(body, ensure_ascii=False)) > 200 else json.dumps(body, ensure_ascii=False)
            print(f">>> body: {preview}")

        result = call_ir(
            target=target, method=method, path=path,
            body=body, port=port, timeout=timeout,
        )
        print_call_result(result["status"], result["elapsed_s"],
                          len(result.get("body", "").encode("utf-8")),
                          result.get("body", ""))
        if result.get("error"):
            print(f"!!! error: {result['error']}")
        results.append({
            "path": path, "method": method.upper(),
            "status": result["status"], "ok": result["success"],
            "body": result.get("body", ""),
            "elapsed_s": result["elapsed_s"],
            "error": result.get("error", ""),
        })

    return {"success": all(r["ok"] for r in results), "results": results}


def print_summary(results):
    """末尾汇总表"""
    print("\n=== 汇总 ===")
    for r in results:
        flag = "OK  " if r["ok"] else "FAIL"
        status = r["status"]
        path = r["path"]
        err = f"  ({r['error']})" if r.get("error") else ""
        print(f"  [{flag}] {status}  {path}{err}")


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("task_file", nargs="?", default=None)
    parser.add_argument("--help", action="store_true")
    args = parser.parse_args()

    if args.help:
        print_help()
        return

    # 定位 inbox/outbox（兼容 scripts/ 和 bin/ 两种布局）
    skill_root = os.path.dirname(SCRIPT_DIR)
    inbox_dir = os.path.join(skill_root, "inbox")
    outbox_dir = os.path.join(skill_root, "outbox")
    os.makedirs(outbox_dir, exist_ok=True)

    # 找要执行的 task 文件
    if args.task_file:
        task_files = [args.task_file]
    else:
        task_files = sorted(glob.glob(os.path.join(inbox_dir, "task_*.json")))

    if not task_files:
        print("inbox/ 下没有 task_*.json 文件")
        return

    for task_file in task_files:
        task_file = os.path.abspath(task_file)
        task_id = os.path.splitext(os.path.basename(task_file))[0]

        with open(task_file, "r", encoding="utf-8") as f:
            task = json.load(f)

        if "task_id" not in task:
            task["task_id"] = task_id

        mode = task.get("mode", "").lower()
        print(f"\n{'='*60}")
        print(f"执行: {task_file}")
        print(f"task_id={task['task_id']}  mode={mode}")
        print(f"{'='*60}")

        if mode == "er":
            result = run_er_task(task, inbox_dir)
        elif mode == "ir":
            result = run_ir_task(task, inbox_dir)
        else:
            print(f"!!! 未知 mode='{mode}', 必须是 'er' 或 'ir'")
            continue

        print_summary(result["results"])

        # 写完整结果到 outbox
        result_file = os.path.join(outbox_dir, f"result_{task['task_id']}.json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"task_id": task["task_id"], **result}, f, ensure_ascii=False, indent=2)
        print(f"\n[完整结果: {result_file}]")


if __name__ == "__main__":
    main()
