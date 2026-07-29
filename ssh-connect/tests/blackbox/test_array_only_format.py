#!/usr/bin/env python3
"""强制数组格式传参 — 黑盒测试。

验证 upload/download 字段：
1. 数组格式（即使只有1个文件）→ 正常执行
2. 单对象格式 → 拒绝执行，错误信息提示必须使用数组
"""

import sys, os, json, subprocess, shutil, tempfile, pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
RUN_PY = os.path.join(SCRIPTS_DIR, 'run.py')
INBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'inbox')
OUTBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'outbox')
TARGET = os.environ.get("SSH_TEST_HOST", "172.18.98.56")


def _write_inbox(data: dict):
    os.makedirs(INBOX_DIR, exist_ok=True)
    task_id = data.get("task_id", "unknown")
    with open(os.path.join(INBOX_DIR, f"task_{task_id}.json"), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def _read_outbox(task_id=None) -> dict:
    if task_id:
        fpath = os.path.join(OUTBOX_DIR, f"result_{task_id}.json")
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                return json.load(f)
    if os.path.exists(OUTBOX_DIR):
        files = [f for f in os.listdir(OUTBOX_DIR)
                 if f.startswith("result_") and f.endswith(".json")]
        if files:
            latest = max(files, key=lambda f: os.path.getmtime(os.path.join(OUTBOX_DIR, f)))
            with open(os.path.join(OUTBOX_DIR, latest), 'r', encoding='utf-8') as f:
                return json.load(f)
    raise FileNotFoundError("outbox 中无 result_*.json")


def _run():
    return subprocess.run([sys.executable, RUN_PY],
                          capture_output=True, text=True, timeout=60, cwd=SCRIPTS_DIR)


def _cleanup():
    for d in [INBOX_DIR, OUTBOX_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)


@pytest.fixture(autouse=True)
def setup_teardown(daemon_session):
    _cleanup()
    yield
    _cleanup()


class TestArrayFormatAccepted:
    """数组格式（含单元素数组）应正常执行。"""

    def test_single_element_array_upload(self):
        tmp = os.path.join(tempfile.gettempdir(), "arr_single.txt")
        with open(tmp, 'w') as f:
            f.write("array_single_content")
        _write_inbox({
            "task_id": "arr_up",
            "target": TARGET,
            "upload": [{"local": tmp.replace("\\", "/"), "remote": "/tmp/arr_single.txt"}]
        })
        _run()
        result = _read_outbox("arr_up")
        assert result["success"] == True, f"单元素数组上传失败: {result}"
        os.remove(tmp)

    def test_single_element_array_download(self):
        tmp = os.path.join(tempfile.gettempdir(), "arr_dl.txt")
        _write_inbox({
            "task_id": "arr_dl",
            "target": TARGET,
            "download": [{"remote": "/tmp/arr_single.txt", "local": tmp.replace("\\", "/")}]
        })
        _run()
        result = _read_outbox("arr_dl")
        assert result["success"] == True, f"单元素数组下载失败: {result}"
        assert os.path.exists(tmp)
        os.remove(tmp)


class TestObjectFormatRejected:
    """单对象格式必须被拒绝，错误信息应提示使用数组。"""

    def test_upload_object_rejected(self):
        _write_inbox({
            "task_id": "obj_up",
            "target": TARGET,
            "upload": {"local": "C:/temp/x.txt", "remote": "/tmp/x.txt"}
        })
        _run()
        result = _read_outbox("obj_up")
        assert result["success"] == False, f"单对象格式应被拒绝: {result}"
        assert "数组" in (result.get("stderr", "") + result.get("error", "")), \
            f"错误信息应提示数组格式: {result}"

    def test_download_object_rejected(self):
        _write_inbox({
            "task_id": "obj_dl",
            "target": TARGET,
            "download": {"remote": "/tmp/x.txt", "local": "C:/temp/x.txt"}
        })
        _run()
        result = _read_outbox("obj_dl")
        assert result["success"] == False, f"单对象格式应被拒绝: {result}"
        assert "数组" in (result.get("stderr", "") + result.get("error", "")), \
            f"错误信息应提示数组格式: {result}"
