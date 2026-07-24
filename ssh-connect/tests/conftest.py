"""pytest 共享配置和 fixtures"""
import pytest
import os
import sys

# 将 scripts 目录加入 path
_scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


def pytest_configure(config):
    """注册自定义 markers"""
    config.addinivalue_line("markers", "integration: 集成测试，需要实际基础设施")
    config.addinivalue_line("markers", "blackbox: 黑盒测试，仅通过 shell 执行")
