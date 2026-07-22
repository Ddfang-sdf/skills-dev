---
name: ssh-connect
description: 连接远程服务器执行命令。当用户需要访问远程服务器、执行远程操作、上传下载文件、查看远程服务状态、远程调试服务时使用此技能。
---

# SSH远程连接

通过SSH协议连接远程服务器并执行命令、上传下载文件。

## 核心原则

**必须主动与用户确认关键信息，不能自作主张！**

## 关键原则

1. **用户决策优先**：连接哪个环境、执行什么操作，必须用户确认
2. **主动询问**：信息不全或存在歧义时，必须询问用户
3. **配置不存在时引导创建**：缺少配置时引导用户创建并保存
4. **错误时请求重新输入**：认证失败等错误，让用户重新输入而不是放弃

## 触发场景（语义识别）

以下场景**自动触发**此skill，**不需要用户明确说"连接"**：

| 用户意图 | 示例语句 |
|----------|----------|
| 查看远程服务状态 | "帮我看看服务器内存"、"查一下test机器的进程" |
| 查看远程日志 | "去生产环境上看下日志"、"帮我看下这台机器的日志" |
| 远程执行命令 | "在服务器上执行下这个命令"、"帮我跑一下这个脚本" |
| 文件传输 | "帮我把这个文件传到服务器上"、"下载服务器上的日志" |
| 远程调试 | "远程debug一下"、"连到服务器上看看什么情况" |
| 服务管理 | "重启下test环境的服务"、"在uat上部署一下" |
| 环境探索 | "登录到服务器看看"、"去test环境检查一下" |

## 环境配置管理

配置文件位置：`scripts/env_config.json`

**配置字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `host` | 是 | 服务器 IP |
| `port` | 否 | SSH 端口，默认 22 |
| `username` | 是 | SSH 用户名 |
| `password` | 是 | SSH 密码 |
| `root_password` | 否 | root/sudo 密码，用于权限提升（见下文） |

已配置的环境：
```json
{
  "environments": {
    "测试环境": {
      "host": "IP地址",
      "port": 22,
      "username": "用户名",
      "password": "密码",
      "root_password": "可选"
    },
    "生产环境": {
      "host": "IP地址",
      "port": 22,
      "username": "用户名",
      "password": "密码",
      "root_password": "可选"
    }
  }
}
```

## 权限提升（自动处理）

当 SSH 账号非 root 或需要更高权限时，配置 `root_password` 可自动处理：

### 自动处理机制

1. **sudo 密码提示**：执行需要 sudo 的命令时，自动检测密码提示并填写 `root_password`
2. **su - 切换**：当命令执行需要 root 权限时，自动切换到 root shell
3. **无感知执行**：用户无需手动 sudo 或输入密码

### 使用场景

- 执行 systemctl 命令（服务管理）
- 绑定 80/443 等特权端口
- 安装系统包、修改系统配置
- 访问受限目录

### 配置示例

```json
{
  "environments": {
    "非root环境": {
      "host": "192.168.1.100",
      "port": 22,
      "username": "deploy",
      "password": "deploy123",
      "root_password": "root_password_here"
    }
  }
}
```

## 连接流程

### 第一步：语义识别（自动触发）

从用户意图中提取：
- **目标环境**：IP、环境名、主机名（如"test机器"、"生产环境"、"7.189.26.215"）
- **操作类型**：命令执行、文件传输、查看状态等
- **目标资源**：具体路径、服务名、文件名等

### 第二步：**确认环境**（必须！）

找到环境配置后，**必须向用户确认**：

```
Agent: "我将连接到 {host}，执行【{操作描述}】。确认继续吗？"
用户: 确认 / 好 / 是 / ...
```

**多种环境匹配时**：
```
Agent: "检测到多个匹配的服务器：
  1. 7.189.26.215
  2. 60.30.18.86
请问要连接哪个？"
```

**用户只说"服务器"等模糊词时**：
```
Agent: "请问要连接哪个服务器？
  1. 7.189.26.215
  2. 60.30.18.86
  3. 其他（请提供IP）"
```

### 第三步：查找配置

根据用户确认的环境，检查 `scripts/env_config.json`：

| 情况 | 动作 |
|------|------|
| 找到配置 | 使用配置信息 |
| **找不到配置** | **进入引导流程** |

### 第四步：引导创建配置

当找不到配置时，**必须**按以下步骤询问用户：

```
步骤1：确认环境名称
Agent: "请为这个连接设置一个名称，例如：测试环境、UAT环境、生产环境"
等待用户回复

步骤2：询问登录凭证
Agent: "请提供登录信息：
- 主机IP: {已知IP}
- 端口: (直接回车使用默认值22)
- 用户名: ?"
等待用户输入用户名

Agent: "密码: ?"
等待用户输入密码（不回显）

步骤3：保存配置
询问用户是否保留已有配置：
```
Agent: "检测到已有配置文件。是否保留已有配置？[是/否]"
```
如果用户选择"是"：
```bash
python -c "import json; f='scripts/env_config.json'; d=json.load(open(f)) if open(f).read() else {'environments':{}}; d['environments']['{新环境名}']={'host':'{IP}','port':22,'username':'{用户名}','password':'{密码}'}; json.dump(d,open(f,'w'),indent=2)"
```
如果用户选择"否"：
```bash
echo '{"environments":{"{新环境名}":{"host":"{IP}","port":22,"username":"{用户名}","password":"{密码}"}}}' > scripts/env_config.json
```

Agent反馈："已保存配置：{环境名} -> {host}:{port}"

步骤4：再次确认
Agent: "已保存。现在连接到【{环境名}】吗？"
```

### 第五步：执行操作

使用 `scripts/smart_connect.py` 执行：

```bash
# 通过环境名执行命令
python scripts/smart_connect.py --json '{"target":"测试环境","command":"ps aux | grep java"}'

# 通过IP执行（自动查找配置）
python scripts/smart_connect.py --json '{"target":"7.189.26.215","command":"df -h"}'

# 直接提供账号密码
python scripts/smart_connect.py --json '{"target":"root/password@192.168.1.100","command":"hostname"}'
```

## 连接后可执行的操作

### 1. 执行命令
```bash
# 查看进程
ps aux | grep {服务名}

# 查看内存
free -h

# 查看磁盘
df -h

# 查看服务状态
systemctl status {服务名}

# 查看日志
tail -100 /opt/oss/log/{服务名}/xxx.log
```

### 2. 上传文件
**注意**：`upload.local` 是 **AI所在Windows机器** 的本地路径（必须是Windows路径如 `C:/Users/...`），`upload.remote` 是 **远程Linux服务器** 的路径（必须是Linux路径如 `/tmp/...`）

```bash
python scripts/smart_connect.py --json '{"target":"测试环境","upload":{"local":"app.jar","remote":"/opt/oss/app.jar"}}'
```

### 3. 下载文件
```bash
python scripts/smart_connect.py --json '{"target":"测试环境","download":{"remote":"/opt/oss/log/xxx.log","local":"./xxx.log"}}'
```

### 4. 列出远程目录
```bash
python scripts/smart_connect.py --json '{"target":"测试环境","command":"ls /opt/oss/log"}'
```

## 错误处理与反馈

| 错误类型 | 错误信息 | Agent动作 |
|----------|----------|-----------|
| 认证失败 | 密码错误，请重新输入 {host} 的 {username} 密码 | 对话："密码错误，请重新输入 {host} 的 {username} 密码" → 等待用户输入 → 重试连接 |
| 连接超时 | 无法连接到 {host}:{port}，请检查网络和防火墙 | 对话："无法连接到 {host}:{port}，请检查网络和防火墙" → 询问是否继续尝试 |
| 连接被拒绝 | 连接被拒绝，端口可能不是22 | 对话："连接被拒绝，是否使用其他端口？" → 等待用户输入 → 重试 |
| 命令执行失败 | 命令执行失败：{error} | 对话："命令执行失败：{error}" → 不自动重试，返回错误信息 |

## 交互示例

### 示例1：确认环境
```
用户: 帮我看看服务器内存
Agent: 检测到多个匹配的服务器：
  1. 7.189.26.215
  2. 60.30.18.86
请问要连接哪个？
用户: 1

Agent: 我将连接到 7.189.26.215，执行【查看内存使用】。确认继续吗？
用户: 确认

Agent: (连接并执行 free -h)
```

### 示例2：直接指定IP
```
用户: 帮我看看7.189.26.215的内存使用情况
Agent: 我将连接到 7.189.26.215，执行【查看内存使用】。确认继续吗？
用户: 好

Agent: (连接并执行 free -h)
```

### 示例3：新环境引导
```
用户: 连到 192.168.1.100 看看情况
Agent: 未找到 192.168.1.100 的配置

Agent: 请提供登录信息：
- 主机IP: 192.168.1.100
- 端口: (直接回车使用默认值22)
- 用户名: ?
用户: devuser
Agent: 密码: ?
用户: (输入密码)

Agent: 已保存配置：192.168.1.100
Agent: 已保存。现在连接到 192.168.1.100 吗？
用户: 是

Agent: (连接并执行)
```

### 示例4：文件传输确认
```
用户: 帮我把这个war包传到7.189.26.215上
Agent: 我将执行以下操作：
  - 操作：上传文件
  - 文件：app.war
  - 目标：7.189.26.215
  - 路径：/opt/oss/app.war
确认继续吗？
用户: 确认
```

## 脚本说明

| 文件 | 作用 |
|------|------|
| smart_connect.py | SSH连接、命令执行、文件传输（支持流式输出） |
| ssh_client.py | SSH连接核心类 |
| env_config.json | 环境配置存储 |

## 调用格式

所有操作通过 `--json` 参数传递一个 JSON 对象：

| 操作 | JSON 格式 |
|------|----------|
| 执行命令 | `{"target":"环境","command":"命令"}` |
| 上传文件 | `{"target":"环境","upload":{"local":"本地路径","remote":"远程路径"}}` |
| 下载文件 | `{"target":"环境","download":{"remote":"远程路径","local":"本地路径"}}` |

**字段说明**：
- `target`：环境名称、IP地址，或 `user/pass@host` 格式
- `command`：要执行的命令
- `timeout`：可选，超时时间（秒），默认 30，最长 3600
- `upload`/`download`：文件传输，与 `command` 互斥
- **自动 TTY**：当 command 以 `mvn`、`npm`、`gradle`、`make`、`configure` 开头时，自动启用 TTY 模式

**target 解析顺序**：
1. 如果匹配 `env_config.json` 中的环境名 → 使用该配置
2. 如果格式为 `user/pass@host` → 直接使用
3. 如果只是 IP 地址 → 查找 `env_config.json` 中 `host` 字段匹配的配置
4. 都不匹配 → 引导用户创建新配置

**local 路径要求**：必须是 Windows 绝对路径，使用正斜杠，如 `C:/Users/xxx/file.txt`

如果用户提供的路径是 Unix 格式（如 `/tmp/a.txt`），AI 应告知用户此路径在 Windows 上不存在，并请用户提供正确的 Windows 路径。

### 常用调用示例

```bash
# 执行命令
python scripts/smart_connect.py --json '{"target":"60.30.18.86","command":"ps aux | grep java"}'

# 上传文件
python scripts/smart_connect.py --json '{"target":"60.30.18.86","upload":{"local":"C:/Users/xxx/downloads/jdk.tar.gz","remote":"/root/jdk.tar.gz"}}'

# 下载文件
python scripts/smart_connect.py --json '{"target":"60.30.18.86","download":{"remote":"/root/logs/xxx.log","local":"./xxx.log"}}'
```

## 重要提醒

1. **必须确认环境**：连接前必须向用户确认要连接哪个环境
2. **多种选择时询问**：多个环境匹配时，让用户选择
3. **必须主动询问**：配置不存在时，不能直接报错退出
4. **密码不回显**：询问密码时使用不回显的输入方式
5. **配置持久化**：用户提供的配置必须保存到env_config.json
6. **错误可逆**：认证失败时让用户重新输入，不是直接放弃
