#!/usr/bin/env python3
"""批量上传/下载 — 黑盒测试。模拟 AI 行为：Write → Run → Read outbox。"""

import sys, os, json, time, socket, subprocess, tempfile, shutil, pytest

# 路径
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
RUN_PY = os.path.join(SCRIPTS_DIR, 'run.py')
INBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'inbox')
OUTBOX_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'outbox')
TARGET = os.environ.get("SSH_TEST_HOST", "172.18.98.56")


def _write_inbox(data: dict):
    os.makedirs(INBOX_DIR, exist_ok=True)
    task_id = data.get("task_id", "unknown")
    fpath = os.path.join(INBOX_DIR, f"task_{task_id}.json")
    with open(fpath, 'w', encoding='utf-8') as f:
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
    return subprocess.run(
        [sys.executable, RUN_PY],
        capture_output=True, text=True, timeout=60,
        cwd=SCRIPTS_DIR
    )


def _cleanup():
    for d in [INBOX_DIR, OUTBOX_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)


@pytest.fixture(autouse=True)
def setup_teardown(daemon_session):
    _cleanup()
    yield
    _cleanup()


# ============================================================
class TestBatchUpload:
    """批量上传：upload 字段为数组时逐个上传，全部成功返回 success=true。"""

    def test_batch_upload_all_success(self):
        # 创建多个临时文件
        tmp_files = []
        for i in range(3):
            f = os.path.join(tempfile.gettempdir(), f"batch_up_{i}.txt")
            with open(f, 'w') as fh:
                fh.write(f"content_{i}")
            tmp_files.append(f)

        _write_inbox({
            "task_id": "batch_up",
            "target": TARGET,
            "upload": [
                {"local": tmp_files[0], "remote": "/tmp/batch_up_0.txt"},
                {"local": tmp_files[1], "remote": "/tmp/batch_up_1.txt"},
                {"local": tmp_files[2], "remote": "/tmp/batch_up_2.txt"},
            ]
        })
        _run()
        result = _read_outbox("batch_up")
        assert result["success"] == True, f"批量上传失败: {result}"
        assert result.get("total") == 3, f"应上传3个文件: {result}"
        assert result.get("failed") == 0, f"不应有失败: {result}"

        for f in tmp_files:
            os.remove(f)


class TestBatchDownload:
    """批量下载：download 字段为数组时逐个下载，全部成功返回 success=true。"""

    def test_batch_download_all_success(self):
        tmp_dl = []
        for i in range(2):
            f = os.path.join(tempfile.gettempdir(), f"batch_dl_{i}.txt")
            tmp_dl.append(f)

        _write_inbox({
            "task_id": "batch_dl",
            "target": TARGET,
            "download": [
                {"remote": "/tmp/batch_up_0.txt", "local": tmp_dl[0]},
                {"remote": "/tmp/batch_up_1.txt", "local": tmp_dl[1]},
            ]
        })
        _run()
        result = _read_outbox("batch_dl")
        assert result["success"] == True, f"批量下载失败: {result}"
        assert result.get("total") == 2
        assert result.get("failed") == 0

        # 验证文件内容
        for i, f in enumerate(tmp_dl):
            with open(f, 'r') as fh:
                assert fh.read() == f"content_{i}", f"文件 {i} 内容不匹配"
            os.remove(f)


class TestBatchPartialFailure:
    """批量上传/下载部分失败时，返回 success=false + failed 计数 + errors 详情。"""

    def test_batch_upload_partial_failure(self):
        tmp_good = os.path.join(tempfile.gettempdir(), "good.txt")
        with open(tmp_good, 'w') as f:
            f.write("ok")

        _write_inbox({
            "task_id": "batch_part",
            "target": TARGET,
            "upload": [
                {"local": tmp_good, "remote": "/tmp/good.txt"},
                {"local": "/nonexistent/file.txt", "remote": "/tmp/bad.txt"},
            ]
        })
        _run()
        result = _read_outbox("batch_part")
        assert result["success"] == False, f"部分失败应返回false: {result}"
        assert result.get("failed") >= 1, f"至少1个失败: {result}"
        assert result.get("total") == 2
        assert "errors" in result, f"应有errors详情: {result}"

        os.remove(tmp_good)


class TestBatchDownloadPartialFailure:
    """批量下载: 部分文件不存在。"""

    def test_batch_download_partial_failure(self):
        tmp_good = os.path.join(tempfile.gettempdir(), "dl_good.txt")
        tmp_bad = os.path.join(tempfile.gettempdir(), "dl_bad.txt")

        _write_inbox({
            "task_id": "batch_dl_part",
            "target": TARGET,
            "download": [
                {"remote": "/tmp/good.txt", "local": tmp_good},
                {"remote": "/nonexistent/bad.txt", "local": tmp_bad},
            ]
        })
        _run()
        result = _read_outbox("batch_dl_part")
        assert result["success"] == False
        assert result.get("failed") >= 1
        assert "errors" in result
        # good文件应该下载成功
        if os.path.exists(tmp_good):
            with open(tmp_good, 'r') as f:
                assert f.read() == "ok"
            os.remove(tmp_good)
