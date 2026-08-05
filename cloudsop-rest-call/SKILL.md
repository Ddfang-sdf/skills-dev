---
name: cloudsop-rest-call
description: 当 AI 在分析问题、排查故障、验证需求时需要调用 CloudSOP / NCE 平台微服务接口获取数据或触发操作时使用此技能。典型场景：定位问题时需要查接口数据、验证需求后需要确认接口返回、分析日志时需要调接口取状态、测试时需要触发服务操作。不是用于"帮我调个接口"这类显式指令。
---

# CloudSOP / NCE 服务接口调用

让 AI 在分析过程中**自主发现**需要调用接口，并透明完成调用，把结果融入分析结论。

## 核心定位

这个 skill 的主体是 AI，不是用户。用户说"帮我定位个问题"、"帮我验证一下需求"，AI 分析后发现需要调接口，就调用。用户看到的是分析结论，不是接口调用过程。

**禁止**：
- 等用户明确说"帮我调 xx 接口"才行动
- 把接口调用细节丢给用户看（status code、body 结构等），除非用户主动要求

**应该**：
- 把接口调用当成分析手段，就像查日志、读文件一样自然
- 调用结果作为论据融入结论，用户关心的是"问题是什么"不是"接口返回了什么"

---

## 何时触发（AI 自主发现）

| 分析场景 | AI 的行动 |
|----------|-----------|
| 用户说"服务好像有问题" | 调 healthcheck 接口确认服务健康状态 |
| 用户说"帮我看下 xx 数据有没有进来" | 调查询接口确认数据存在 |
| 用户说"需求做完了，帮我测试一下" | 调接口验证功能行为符合预期 |
| 用户说"日志里报错了" | 查相关接口状态辅助定位 |
| 用户说"页面上数据不对" | 调接口对比实际数据 |

## 调用方式

所有调用统一三步：

### 第一步：Write 任务文件

`<SKILL_ROOT>/inbox/{task_id}.json`，文件名即 task_id。

### 第二步：执行

- exe 包：`<SKILL_ROOT>/bin/rest-run.exe`

### 第三步：获取结果

- stdout 末尾有 `[完整结果: outbox/result_{task_id}.json]` → 输出完整
- 截断或信息不全 → Read 工具读 `outbox/result_{task_id}.json`

---

## ER 还是 IR

| 场景 | 判断 |
|------|------|
| 接口路径来自用户本地代码仓的 yaml | 看服务性质：对外页面用 → ER；纯内部管理 → IR |
| 接口定义在 NCE 服务器上（微服务自带 yaml） | 多半是 IR |
| 明确给了端口号 | 31943 → ER；32018 → IR |
| 不确定 | 问用户："这个接口是 ER(31943,对外) 还是 IR(32018,内部)？" |

**ER**（31943）：AI 本机直连，无需 ssh-connect。
**IR**（32018）：必须在 NCE 管理节点上以 ossadm 身份调用，借道 ssh-connect skill。

---

## Task 文件格式

### ER 模式

```json
{
    "task_id": "task_1",
    "mode": "er",
    "target": "7.222.36.7",
    "user": "admin",
    "pwd": "Changeme_123",
    "calls": [
        {"method": "POST", "path": "/rest/seceyecommonsituationservice/v1/event/management/count-query", "body_file": "body_1.json"},
        {"method": "GET",  "path": "/rest/seceyecommonsituationservice/v1/healthcheck"}
    ]
}
```

- `mode`: `"er"`，必填
- `target`: 目标 IP
- `user`/`pwd`: ER 账号，首次使用询问用户，默认 `admin/Changeme_123`
- `calls`: 数组，一次登录批量调多个接口（复用 cookie）
- `body_file`: 相对 task 文件所在目录；也支持 `body` 字段内联 JSON

### IR 模式

```json
{
    "task_id": "task_2",
    "mode": "ir",
    "target": "7.222.36.7",
    "calls": [
        {"method": "GET",  "path": "/rest/seceyecommonsituationservice/v1/healthcheck"},
        {"method": "POST", "path": "/rest/someservice/v1/foo", "body": {"k": "v"}}
    ]
}
```

- `mode`: `"ir"`，必填
- `target`: ssh-connect 里已录入的环境名（即 env_name）
- `user`/`pwd` 不填——IR 用 SSH 凭证，从 ssh-connect 环境配置读
- `body` 直接写 dict，脚本自动序列化

---

## 接口定义从哪来

AI 分析过程中发现需要调接口时，接口路径来源：

- **用户直接给 URL**：如 `/rest/seceyecommonsituationservice/v1/healthcheck` —— 直接用
- **本地代码仓的 yaml**：用 Glob 找 `<repo>/model/src/main/resources/rest-*.yaml`，列出文件名让用户选要调哪个服务的接口
- **完整路径** = `basePath` + `path`，例：`/rest/seceyecommonsituationservice` + `/v1/healthcheck`

NCE 微服务接口定义文件命名规律：`rest-{service-name}.yaml`

---

## 结果如何呈现

**不要**给用户看接口原始响应，除非用户主动要求。

**应该**：把接口结果翻译成分析结论。

```
❌ 错误示范：
"接口返回 status=200, body={"count": 42}"

✅ 正确示范：
"查了事件接口，当前环境有 42 条未处理事件，
和你说的情况吻合。下一步建议看 xx 日志。"
```

批量调用时，AI 自行汇总分析，只给用户结论：

```
✅ 正确示范：
"6 个接口里 5 个正常，事件查询接口返回 500。
body 里报 NullPointerException，疑似是 xxx 表没初始化。
建议先检查初始化脚本是否执行过。"
```

---

## 错误处理

| 信号 | 含义 | 处置 |
|------|------|------|
| ER: TCP 探测失败 | 网络不通/VPN 没连 | 提示用户检查网络 |
| **ER: 登录失败** | 账号密码错 | **必须询问用户重新提供凭证，更新 task 文件后重试** |
| ER: 401 | cookie 过期 | 脚本自动重登一次，仍失败则报告 |
| IR: 未找到 target | 环境未录入 ssh-connect | 引导用户先录 SSH 凭证 |
| IR: SSH 密码错 | SSH 凭证失效 | 询问用户更新凭证 |
| IR: STATUS 404 | 路径不对 | 核对 basePath + path |
| IR: STATUS 503 | 服务未就绪 | 建议先 healthcheck 确认 |

**登录失败处理流程（必须遵守）**：

1. 向用户说明："登录失败，账号密码可能错误"
2. 询问用户正确的用户名和密码
3. 重写同名 task 文件（更新 user/pwd）
4. 重新执行
5. 仍失败 → 报告用户，不要继续猜测

---

## 前置依赖

- **ER 模式**：无需额外依赖，本机直连
- **IR 模式**：ssh-connect skill 必须已安装在本 skill 同级目录（`<skills_root>/ssh-connect/`）。找不到时报错提示用户安装。
- **IR 调用过程**：会向 ssh-connect 的 inbox 写入临时 task 文件（探测 IR IP 用），执行后由 ssh-connect 自动清理。如果看到 ssh-connect inbox 里有 `task_ir_probe_xxx.json` 残留，说明上次调用中途失败，删除即可。

---

## 安全须知

- ER 凭证明文存 task 文件，执行后自动删除（脚本已处理）
- IR 借道 ssh-connect，危险命令由其 command_guard 拦截，不要拆命令绕过
- 不要用 `escalate: true` 跑 IR，`su ossadm -c` 是正确权限模型

---

## 交互示例

### 示例 1：问题定位中自主调用接口

```
用户: 帮我看下事件统计接口好像返回不对
AI: 我先查一下当前环境的事件数据状态。
    [Write inbox/task_1.json mode=er]
    [Run rest-run.exe]
    → 接口返回 200，count=42
    → 但用户说页面显示 0
AI: 接口数据正常（42 条），但页面没展示。
    可能是前端过滤条件问题，我看看调用参数。
```

### 示例 2：需求验证中自主调用接口

```
用户: 事件上报的需求做完了，帮我测一下
AI: 我调一下事件查询接口验证数据是否正确入库。
    [Write inbox/task_1.json mode=er]
    [Run rest-run.exe]
    → status=200，返回最新事件 id=SRTEST-001
AI: 数据已正确入库。再验证一下详情页接口。
    [Write inbox/task_2.json]
    [Run rest-run.exe] → 200
AI: 验证通过，数据入库和详情页都正常。
```

### 示例 3：服务健康检查

```
用户: 环境上服务好像起不来
AI: 我调一下 healthcheck 确认服务状态。
    [Write inbox/task_1.json mode=er]
    [Run rest-run.exe]
    → status=503
AI: 服务还没起来（503），看下日志定位原因。
```

### 示例 4：登录失败处理

```
用户: 帮我验证一下需求
AI: 我调接口验证数据。
    [Write inbox/task_1.json mode=er]
    [Run rest-run.exe]
    → 登录失败: validateUser status=401
AI: 登录失败，账号密码可能错误。请提供正确的用户名和密码。
用户: 用户名 admin，密码是 NewPassword456
AI: [Write inbox/task_1.json 覆写，更新 user/pwd]
    [Run rest-run.exe]
    → status=200
AI: 验证通过，数据正常。
```

---

## Reference Files

- `references/er_login_flow.md` —— ER 登录链路细节（仅排查登录失败时读）
- `references/ir_command_escaping.md` —— IR 三层引号转义规则（仅排查命令构造问题时读）
- `references/interface_definition_format.md` —— NCE swagger/yaml 接口定义文件格式速查
- `scripts/rest_run.py` —— 主调度脚本（黑盒，先 `--help`）
- `scripts/er_login.py` —— ER 登录器（可独立拿 cookie/roarand）
- `scripts/ir_caller.py` —— IR 调用器（构造 su ossadm 命令 + 调 ssh-connect）
