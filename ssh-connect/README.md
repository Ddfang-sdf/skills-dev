# SSH Remote Connection Skill

一个 Claude Code Skill，让 AI 通过 SSH 连接远程 Linux 服务器执行命令、上传/下载文件。后台 daemon 维持长连接避免重复登录，内置危险命令拦截和 sudo 提权。

## 功能概览

- **文件桥接模式** — AI 通过文件传递命令，消除跨 Shell 转义问题
- **长连接复用** — 后台 daemon 维持 SSH 连接池，同一服务器多次操作只登录一次
- **危险命令拦截** — BLOCK/WARN/ALLOW 三级安全检查，`force=true` 需用户确认
- **多凭证支持** — 一个环境配置多组账号（如 admin/root），`as` 字段切换
- **sudo 提权** — `escalate: true` 走 `sudo -S` 管道送密码
- **跳板机穿透** — `via` 字段声明跳板关系，direct-tcpip 隧道透明
- **环境管理** — 通过 `env` 操作管理服务器配置，脚本校验写入
- **host key 校验** — known_hosts accept-new 机制，密钥变更拒绝连接
- **PyInstaller 打包** — 提供 exe 版本，无需安装 Python

## 目录结构

```
ssh-connect/
├── SKILL.md          # Skill 定义文件（AI 行为规范）
├── README.md         # 本文件
├── bin/              # exe 可执行文件
│   ├── ssh-run.exe
│   └── ssh-daemon.exe
├── scripts/          # Python 源码
│   ├── run.py            # CLI 入口
│   ├── ssh_daemon.py     # 后台守护进程
│   ├── ssh_session.py    # SSH 会话封装
│   ├── command_guard.py  # 命令安全检查
│   └── env_config.json   # 环境配置
├── tests/            # 测试代码
└── inbox/ outbox/    # AI 与脚本的桥接目录（运行时自动创建）
```

## 安装方式

### 方式一：exe 版本（推荐，无需安装 Python）

1. 下载 `ssh-connect/` 整个目录，放到项目根目录下
2. 确保 `bin/ssh-run.exe` 和 `bin/ssh-daemon.exe` 存在
3. 完成，AI 调用 `ssh-connect/bin/ssh-run.exe`

### 方式二：源码版本（需要 Python 3.x）

1. 下载 `ssh-connect/` 整个目录，放到项目根目录下
2. 安装依赖：
   ```bash
   pip install paramiko
   ```
3. 完成，AI 调用 `python ssh-connect/scripts/run.py`

### 方式三：从源码构建 exe

```bash
pip install pyinstaller
cd ssh-connect/scripts

# 构建
python -m PyInstaller --onefile --name ssh-run --distpath ../bin run.py
python -m PyInstaller --onefile --name ssh-daemon --distpath ../bin ssh_daemon.py
```

## 配置服务器

AI 通过对话引导你录入服务器信息。你也可以手动编辑 `scripts/env_config.json`：

```json
{
    "daemon": {
        "host": "127.0.0.1",
        "port": 19522,
        "session_idle_timeout": 300,
        "heartbeat_interval": 60
    },
    "environments": {
        "10.0.1.5": {
            "host": "10.0.1.5",
            "port": 22,
            "credentials": [
                {
                    "name": "admin",
                    "username": "admin",
                    "password": "你的密码",
                    "sudo_password": "sudo密码（可选）"
                },
                {
                    "name": "root",
                    "username": "root",
                    "password": "root密码"
                }
            ],
            "default_credential": "admin",
            "via": "203.0.113.1"
        },
        "203.0.113.1": {
            "host": "203.0.113.1",
            "credentials": [
                {
                    "name": "ops",
                    "username": "ops",
                    "password": "跳板机密码"
                }
            ],
            "default_credential": "ops"
        }
    }
}
```

- 环境的 key 是服务器 IP，如 `"10.0.1.5"`
- `credentials` 数组可包含多组凭证，`as` 字段选择使用哪组
- `default_credential` 指定不写 `as` 时默认用哪组
- `via` 填跳板机 IP，需先录入跳板机

**强烈建议通过 AI 对话录入**：告诉 AI "帮我添加 10.0.1.5 这个服务器"，AI 会询问凭证并通过 `env` 操作写入，避免手动 JSON 格式错误。

## 对 AI 说的话

### 日常使用

对 AI 说自然语言即可：

- "帮我看看 10.0.1.5 的内存使用情况"
- "在 10.0.1.5 上重启 nginx"
- "把这几个文件传到 10.0.1.5 的 /opt/app 目录下"
- "帮我添加 10.20.30.40 这台服务器的登录信息"

### AI 具体调用流程

1. AI 使用 Write 工具覆写 `ssh-connect/inbox/task.json`
2. AI 调用 `ssh-connect/bin/ssh-run.exe`（exe）或 `python ssh-connect/scripts/run.py`（源码）
3. AI 读取 stdout 或 `ssh-connect/outbox/result.json` 获取结果

### 任务 JSON 格式速查

**执行命令**：
```json
{"task_id":"task_1","target":"10.0.1.5","command":"free -h","timeout":30}
```

**上传文件**：
```json
{"task_id":"task_2","target":"10.0.1.5","upload":{"local":"C:/Users/.../app.jar","remote":"/opt/app.jar"}}
```

**下载文件**：
```json
{"task_id":"task_3","target":"10.0.1.5","download":{"remote":"/opt/logs/app.log","local":"C:/Users/.../app.log"}}
```

**添加环境**：
```json
{"task_id":"task_4","env":"add","env_name":"10.20.30.40","config":{...}}
```

更多格式见 [SKILL.md](SKILL.md)。

## 安全机制

| 层级 | 机制 | 说明 |
|------|------|------|
| 命令 | BLOCK/WARN/ALLOW 三级拦截 | BLOCK 级（`rm -rf /`）始终拒绝，WARN 级需用户确认 |
| 连接 | known_hosts accept-new | 首次信任并记录指纹，密钥变更拒绝连接（防 MITM） |
| 进程 | daemon.token 鉴权 | 本机 token 验证，防止其他进程借用 daemon |
| 提权 | 不自动 sudo | 权限不足时 AI 判断是否 escalate，改 sshd_config 需用户确认 |
| 可见性 | Write 工具展示 diff | 每次执行命令，用户可在 Agent 界面看到命令内容 |

## 常见问题

**Q: daemon 是什么？需要我手动启动吗？**
A: daemon 是后台进程，维持 SSH 连接池。AI 首次调用脚本时会自动启动。重启电脑后需要重新启动（AI 下次调用时会自动拉起）。

**Q: exe 和源码版本有什么区别？**
A: 功能完全相同。exe 版本无需安装 Python/paramiko，开箱即用；源码版本需要 Python 3.x + paramiko，但可以修改代码。

**Q: 密码存在哪？安全吗？**
A: 存在 `scripts/env_config.json`，明文存储。依赖文件系统权限保护。不要在共享环境使用。

**Q: 如何支持跳板机？**
A: 先录入跳板机（如 `"203.0.113.1"`），再在目标环境的 `via` 字段引用。AI 执行命令时自动穿透，无需 AI 感知。

**Q: 如何提权执行命令？**
A: 在 credential 中配置 `sudo_password`。执行命令后如果权限不足，AI 会设 `escalate: true` 重试，走 `sudo -S` 管道送密码。

## 开发

```bash
cd ssh-connect
pip install paramiko pytest

# 运行单元测试
python -m pytest tests/unit/ -v

# 配置 WSL 作为测试环境
# 1. WSL 中安装 openssh: apk add openssh
# 2. 启动 sshd，设置 root 密码
# 3. 修改 scripts/env_config.json 中的 IP 为 WSL IP

# 运行全部测试
SSH_TEST_HOST=172.x.x.x SSH_TEST_USER=root SSH_TEST_PASS=xxx SSH_TEST_SUDO_PASS=xxx \
  python -m pytest tests/ -v
```
