# IR 三层引号转义规则

> 仅在排查 IR 命令构造问题时读。正常调用看 SKILL.md 即可，`scripts/ir_caller.py::_build_shell_command` 已实现这套转义。

## 概述

IR 调用通过 ssh-connect 下发一条 shell 命令到管理节点，命令里嵌 python 代码。整条命令要穿过三层解析：

```
JSON 字符串 (task 文件)
  └─ shell 命令 (su ossadm -c "...")
       └─ python 代码 (python -c '...')
```

每穿过一层，引号都要多转义一次。**永远让 `ir_caller.py` 生成命令，不要手写**——三层转义极易出错。

## 三层结构解剖

以调用 `GET /rest/xxx/v1/healthcheck` 为例：

### 第 1 层: JSON 字符串（task 文件）

```json
{
  "task_id": "task_ir_1",
  "target": "1.1.1.1",
  "command": "su ossadm -c \". /opt/*/manager/agent/bin/engr_profile.sh && python -c 'from util import common, httpclient; headers={\\\"Content-Type\\\":\\\"application/json\\\"}; client=httpclient.CommonHttpClient(common.get_local_ip(), 32018, True, False, headers=headers); status, response=client.get(\\\"/rest/xxx/v1/healthcheck\\\"); print(\\\"STATUS:\\\", status); print(\\\"BODY:\\\", str(response, encoding=\\\"utf-8\\\"))'\"",
  "timeout": 120
}
```

JSON 字符串里的 `\"` 是字面双引号，`\\\"` 是字面反斜杠+双引号（穿过 JSON 后变成 `\"`，再穿过 shell 后变成 `"`）。

### 第 2 层: shell 命令（JSON 解析后）

```bash
su ossadm -c ". /opt/*/manager/agent/bin/engr_profile.sh && python -c 'from util import common, httpclient; headers={\"Content-Type\":\"application/json\"}; client=httpclient.CommonHttpClient(common.get_local_ip(), 32018, True, False, headers=headers); status, response=client.get(\"/rest/xxx/v1/healthcheck\"); print(\"STATUS:\", status); print(\"BODY:\", str(response, encoding=\"utf-8\"))'"
```

- `su ossadm -c "..."` 的双引号是 shell 双引号
- shell 双引号内的 `\"` 被 shell 解释为字面 `"`
- python 片段用单引号 `'...'` 包裹，shell 不干预单引号内的内容

### 第 3 层: python 代码（shell 执行后）

```python
from util import common, httpclient; headers={"Content-Type":"application/json"}; client=httpclient.CommonHttpClient(common.get_local_ip(), 32018, True, False, headers=headers); status, response=client.get("/rest/xxx/v1/healthcheck"); print("STATUS:", status); print("BODY:", str(response, encoding="utf-8"))
```

python 看到的是干净的代码，`"..."` 是普通双引号字符串。

## 转义规则速查表

| 层 | 字符 | 在该层的表示 | 穿过该层后变成 |
|----|------|-------------|---------------|
| JSON | `"` | `\"` | `"` |
| JSON | `\` | `\\` | `\` |
| JSON→shell | `"` (要进 shell 双引号内) | `\\\"` | `\"` → `"` |
| shell | `"` (在双引号内) | `\"` | `"` |
| shell | `'` (单引号) | `'` (不转义) | `'` |
| python | `"` (字符串字面量) | `\"` (在单引号包裹内) | `"` |

## POST/PUT/PATCH body 的额外转义

body 是 JSON 字符串，要嵌进 python 字符串字面量。body 里的 `"` 要转义为 `\"`：

```python
# body = {"k":"v"}
# python 字符串字面量: "{\"k\":\"v\"}"
# 嵌进 shell 双引号:    "{\\\"k\\\":\\\"v\\\"}"
# 嵌进 JSON:           "{\\\\\\\"k\\\\\\\":\\\\\\\"v\\\\\\\"}"
```

**不要手算**——`ir_caller.py::_build_python_snippet` 已经用 `.replace('\\', '\\\\').replace('"', '\\"')` 处理了。

## 验证转义正确的方法

写完 task 文件后，用 python 验证 JSON 合法性：

```bash
python -c "import json; t=json.load(open('task_ir_1.json')); print(t['command'])"
```

输出的 `command` 字段就是 shell 会看到的命令。再粘贴到本地 shell（不通过 ssh-connect）看 python 能不能解析：

```bash
# 模拟（不要真跑，python -c 里是产品代码）
echo '<command 字符串>' | python -c "import sys; print(sys.stdin.read())"
```

## 常见错误信号

| 错误 | 原因 |
|------|------|
| `LocationParseError: Failed to parse` | URL 路径被 Git Bash MSYS 转换（`/rest/...` → `D:/.../rest/...`），不是引号问题 |
| `SyntaxError: invalid syntax` (python) | 引号转义层数不对 |
| `SyntaxError: EOL while scanning string literal` | 字符串没闭合，多半是 `\"` 数量错 |
| `sh: -c: line 0: unexpected EOF` | shell 双引号没闭合 |
| `json.decoder.JSONDecodeError` | task 文件 JSON 不合法 |

## 参考

- `scripts/ir_caller.py::_build_python_snippet` —— 生成 python 片段
- `scripts/ir_caller.py::_build_shell_command` —— 包裹 shell + JSON 转义
- SshNBBox 仓 `ui/uitools/SshRestUI.py:1087-1101` —— "转录模式"单行命令模板（本方案的灵感来源）
