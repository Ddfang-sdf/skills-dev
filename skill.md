---
name: cloudsop-rest-call
description: 调用 CloudSOP / NCE 平台微服务的 ER (31943, 外部 HTTPS) 和 IR (32018, 内部) REST 接口并查看 response。当用户提到"调用 ER 接口"、"调用 IR 接口"、"调一下微服务接口"、"看看接口返回"、"测试 REST 接口"、"发个 POST/GET 到 NCE"、"CloudSOP 接口"、"SecEye 接口"、"看接口响应"、"调 ER/IR"、给出形如 /rest/xxx/v1/yyy 的 URL 路径要发请求，或想让 AI 替代 Postman 调 NCE 接口时，使用此 skill。也适用于用户给出 swagger/yaml 接口定义文件并希望实际调用其中接口的场景。不触发于：单纯读/编辑 yaml 文件、问接口定义含义、改代码里的接口实现、调非 NCE/CloudSOP 的第三方 REST API。
---

# CloudSOP / NCE REST 接口调用

让 AI 替代 Postman，调用 NCE/CloudSOP 平台微服务的 **ER**（External Request, 31943）和 **IR**（Internal Request, 32018）接口，拿到 response 给用户看。

两类接口机制完全不同，**第一步永远是分类**——发错端口会卡在鉴权或网络不通上浪费时间。

## 决策树：先分清 ER 还是 IR

```
用户给的 URL / 接口需求
  │
  ├─ 端口 31943 / 明确说 "ER" / 来自对外应用面 → 走 ER 流程（本机 requests）
  │
  ├─ 端口 32018 / 明确说 "IR" / 来自微服务内部 yaml → 走 IR 流程（ssh-connect）
  │
  └─ 没说端口，只给了 /rest/... 路径
       │
       ├─ 接口定义文件 (swagger/yaml) 在 NCE 服务器上 (微服务自带) → 多半是 IR
       ├─ 接口定义文件在用户本地代码仓库 (如 situation_workspace) → 看服务性质：
       │     对外页面会用 → 试 ER
       │     纯内部管理类 → 试 IR
       └─ 还是不确定 → 先问用户："这个接口是 ER(31943,对外) 还是 IR(32018,内部)？"
```

| 维度 | ER (31943) | IR (32018) |
|------|------------|------------|
| 网络 | 外部 HTTPS，AI 本机直连 | 内部，**必须**在管理节点本机调用 |
| 鉴权 | 5 步 bspsession/roarand 登录链 | 无（内部信任 + SSL_ROOT 双向 TLS） |
| 工具 | 本机 `requests`（不需要 ssh-connect） | **复用 ssh-connect skill**（su ossadm + python CommonHttpClient） |
| 凭证 | API 账号 `admin/Changeme_123` | SSH 账号（root 或 ossadm） |
| 调用方身份 | 任何能访问 31943 的机器 | 管理节点上的 ossadm 用户 |

---

## 通用约定

无论 ER 还是 IR，调用前都要先从用户那里拿全四要素：**目标 IP / 凭证 / HTTP method / 接口路径**。缺一个都要先问清楚再动。

### Task 文件 + 黑盒脚本模型

两类调用都走 **inbox 任务文件 → 跑脚本 → 读 outbox 结果** 的三步模式（与 ssh-connect skill 一致，用户已熟悉）：

1. **Write** `<SKILL_ROOT>/inbox/task_{id}.json` —— 任务文件，文件名即 task_id
2. **Run** `<SKILL_ROOT>/bin/rest-run.exe`（不存在则 `python <SKILL_ROOT>/scripts/rest_run.py`）
3. **Read** stdout 末尾的 `[完整结果: outbox/result_{id}.json]` 标记；缺标记或要详情就读 result 文件

`scripts/rest_run.py` 是黑盒——**先 `--help` 看用法，不要读源码**，除非确实要改。它内部已经处理了登录、转义、错误分类、输出格式化。

### Task ID 规则

- 格式 `task_N`（N 从 1 递增）
- 新任务用新 task_id，重试/补参数**沿用同一 task_id**（覆写文件）

---

## ER 流程（端口 31943，本机直连）

ER 是 NCE 对外应用面端口，AI 本机用 `requests` 直连 HTTPS。鉴权是一套 5 步登录链路，最终拿到 `bspsession` (cookie) + `roarand` (csrf token) 塞进 header，和你用 Postman 时从浏览器 devtools 抄这两值填 header 是同一回事——skill 只是自动化了这套手工流程。

### 何时问用户要 ER 账号

首次对一个新 IP 调 ER 时，用 AskUserQuestion 确认账号，**默认填 `admin / Changeme_123`** 让用户确认或修改。不要盲发——在错环境上烧账号会触发账户锁定，且日志会脏。已确认过的 IP 可直接复用（脚本会缓存）。

### ER Task 文件

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

- `mode: "er"` 必填，区分 IR
- `calls` 是数组，支持一次登录批量调多个接口（复用同一 cookie/roarand，与 Postman 行为一致）
- `body_file` 相对 task 文件所在目录；也支持 `body` 字段内联 JSON（str 或 dict）
- **body 永远走文件或内联 JSON 字段，不要塞 CLI 参数**——长 payload 会撑爆命令行，且 Git Bash 会把 `/rest/...` 路径误转成 `D:/emviroment/Git/rest/...`（MSYS 路径转换坑）

### ER 脚本做了什么（黑盒概要）

1. 5 步登录：validateUser → 跟随重定向拿 bspsession → license 页 → csrfToken → licensedirectlogin
2. 用同一 `requests.session()` 复用 cookie 调每个 call
3. 每个调用打印 `status / time / size / body`，末尾汇总表
4. 401 时自动重登一次重试

登录链路细节见 `references/er_login_flow.md`（仅在排查登录失败时读）。

---

## IR 流程（端口 32018，借道 ssh-connect）

IR 是内部端口，**AI 本机访问不到**，必须在管理节点上以 ossadm 身份调产品自带的 `util.httpclient.CommonHttpClient`（它处理双向 TLS + SSL_ROOT 环境变量）。所以 IR 借道 **ssh-connect skill** 下发命令。

### 前置依赖

- IR 调用前，目标环境的 SSH 凭证必须已录入 ssh-connect skill（`env add`）。如果用户还没录，先引导他录：root 或 ossadm 都行，root 会自动 `su ossadm -c` 切换。
- 不要自己造 SSH 连接——ssh-connect skill 已经维护了 daemon 长连接、危险命令拦截、凭证管理，复用它。

### IR Task 文件

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

- `mode: "ir"` 必填
- `target` 是 ssh-connect 里已录入的 env_name
- `user`/`pwd` 不填——IR 用 SSH 凭证，从 ssh-connect env 配置读
- `body` 直接写 dict，脚本会自动 `json.dumps` 序列化（不要用户预序列化）

### IR 脚本做了什么（黑盒概要）

1. 先探 `cat /opt/*/manager/var/agent/managerip.conf | grep localip` 拿 IR IP
2. 对每个 call 构造单行 shell 命令：
   ```
   su ossadm -c ". /opt/*/manager/agent/bin/engr_profile.sh && python -c 'from util import common, httpclient; ...'"
   ```
3. 写到 ssh-connect 的 inbox，调 `ssh-run.exe`，读结果
4. 解析 python stdout 的 `STATUS:` / `BODY:` 行
5. R21C10 版本差异自动降级：`get_local_ip` 失败回退 `getLocalIP`

**三层引号转义**（JSON → shell → python）是 IR 最大的坑，**永远让脚本生成命令，不要手写**。转义规则见 `references/ir_command_escaping.md`（仅在排查命令构造问题时读）。

---

## 接口定义从哪来

用户通常会给以下之一：
- **直接给 URL 路径**：如 `/rest/seceyecommonsituationservice/v1/healthcheck` —— 直接用
- **给 swagger/yaml 文件**：读文件取 `basePath` + `paths`，拼完整路径。例：basePath=`/rest/seceyecommonsituationservice`、path=`/v1/healthcheck` → 完整路径 `/rest/seceyecommonsituationservice/v1/healthcheck`
- **本地代码仓库的 resources 目录**（如 `D:\workspace\projects\code\situation_workspace\SecEyeCommonSituationService\model\src\main\resources\rest-*.yaml`）—— 列出所有 yaml 让用户选一个

NCE/CloudSOP 的微服务接口定义文件命名规律：`rest-{service-name}.yaml`，每个文件是一个服务的所有接口。

---

## 错误分类与处置

按信号定位根因，不要盲目重试：

| 信号 | 含义 | 处置 |
|------|------|------|
| **ER 类** | | |
| TCP 探测失败 | 网络不通 / VPN 没连 | 提示用户检查 IP/端口/VPN |
| `validateUser` status != 200 | 账号密码错 | 询问用户更新 ER 账号 |
| `bspsession` 为空 | 会话建立失败 | 看 validateUser response body，多半还是账号问题 |
| `csrfToken` 为空 | session 接口异常 | 看 response body，可能环境异常 |
| 真实请求 401 | cookie/roarand 过期 | 脚本自动重登一次；仍失败则报告用户 |
| 真实请求 4xx/5xx | 业务错误 | 透传 body 给用户，不要替用户猜原因 |
| **IR 类** | | |
| ssh-connect "未找到 target" | 环境未录入 ssh-connect | 引导用户先 `env add` 录 SSH 凭证 |
| `AUTH_FAILED` | SSH 密码错 | 询问用户更新 ssh-connect 凭证 |
| python `AttributeError: get_local_ip` | R21C10 之前版本 | 脚本自动降级到 `getLocalIP`，无需人工 |
| python `ImportError: util.httpclient` | `engr_profile.sh` 没 source 成功 / 路径不对 | 检查 `/opt/*/manager/agent/bin/engr_profile.sh` 存在性 |
| `STATUS: 404` | IR 路径不对 / 服务未注册 | 核对 basePath + path 拼接 |
| `STATUS: 503` | 服务未就绪 | 建议 healthcheck 先确认服务健康 |
| 连接超时 | IR 端口未监听 / localip 取错 | 跑 `netstat -tnlp \| grep 32018` 确认监听 |
| 空 BODY 但 STATUS 200 | 正常（healthcheck 等接口就是这样） | 不要当错误，状态码是真相 |

---

## 输出格式（与 Postman 心智对齐）

每次调用都按这个格式呈现给用户，让他像看 Postman 响应一样：

```
ER POST https://7.222.36.7:31943/rest/.../count-query
status=200  time=142 ms  size=31 bytes
{
  "count": 1,
  "isExceedMax": false
}
```

```
IR GET https://7.222.36.7:32018/rest/.../healthcheck
status=200  time=1.08 s  size=0 bytes
(empty body — healthcheck only returns 200)
```

批量调用末尾加汇总表：
```
=== 汇总 ===
  [OK]   200  /rest/.../count-query
  [FAIL] 503  /rest/.../healthcheck
```

---

## 安全须知（请主动告知用户）

- **ER 凭证明文存 task 文件**：inbox 里的 task_*.json 含明文密码，调完建议清掉 inbox 历史文件
- **凭证会经过 LLM 云端**：录入和调用内容会经过模型处理，敏感环境自评风险
- **IR 借道 ssh-connect 的危险命令拦截**：IR 调用本身不会触发拦截，但若在脚本里夹带 `rm`/`kill` 等会被 ssh-connect 的 command_guard 拦——按其 force 纪律走流程，**不要拆命令绕过**
- **不要用 `escalate: true` 跑 IR**：IR 不是 sudo 场景，`su ossadm -c` 是正确权限模型，加 escalate 会破坏 ossadm 环境

---

## 交互示例

### 示例 1：用户给 ER URL 直接调

```
用户: 调一下 7.222.36.7 的 ER 接口 POST /rest/seceyecommonsituationservice/v1/event/management/count-query
      body 是 {"startTime":...,"endTime":...}
AI: 首次对 7.222.36.7 调 ER，账号默认 admin/Changeme_123，确认使用吗？
用户: 是
AI: [Write inbox/task_1.json + body_1.json]
    [Run rest-run.exe]
    → status=200 time=142ms
AI: 调用成功，返回 {"count":1,"isExceedMax":false}
```

### 示例 2：用户给本地 yaml 接口定义

```
用户: 用 D:\...\rest-services-healthcheck.yaml 这个接口定义调一下 IR
AI: [Read yaml] → GET /rest/seceyecommonsituationservice/v1/healthcheck
    这是 IR 接口（在 NCE 服务器上的微服务 yaml），走 ssh-connect。
    7.222.36.7 在 ssh-connect 里录过凭证吗？
用户: 录过 root
AI: [Write inbox/task_1.json mode=ir]
    [Run rest-run.exe] → 内部调 ssh-run.exe
    → status=200 time=1.08s
AI: 调用成功，healthcheck 返回 200，服务健康。
```

### 示例 3：批量调 6 个接口

```
用户: 这 6 个 ER 接口都调一下（贴了 6 个 URL+payload）
AI: [Write inbox/task_1.json with 6 calls + 6 body files]
    [Run rest-run.exe] → 一次登录，6 个调用复用 cookie
    → 汇总表
AI: 6 个里 5 个 200，1 个 500。500 的是 xxx，body 是 yyy，要不要看服务日志？
```

---

## Reference Files

- `references/er_login_flow.md` —— ER 5 步登录链路细节（仅排查登录失败时读）
- `references/ir_command_escaping.md` —— IR 三层引号转义规则（仅排查命令构造问题时读）
- `references/interface_definition_format.md` —— NCE swagger/yaml 接口定义文件格式速查
- `scripts/rest_run.py` —— 主调度脚本（黑盒，先 `--help`）
- `scripts/er_login.py` —— ER 登录器（可独立拿 cookie/roarand）
- `scripts/ir_caller.py` —— IR 调用器（构造 su ossadm 命令 + 调 ssh-connect）
