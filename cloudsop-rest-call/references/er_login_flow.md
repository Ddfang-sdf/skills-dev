# ER 5 步登录链路细节

> 仅在排查 ER 登录失败时读。正常调用看 SKILL.md 即可，`scripts/er_login.py` 已实现这套流程。

## 概述

ER (31943) 是 cloudsop 对外应用面端口，鉴权是一套 5 步 HTTP 重定向链路，最终拿到两个值塞进 header：

| 值 | 来源 | 用途 |
|----|------|------|
| `bspsession` | step 2 的 cookie | 会话标识，塞进 `Cookie` header |
| `csrfToken` (roarand) | step 4 的 JSON 响应 | CSRF 防护，塞进 `roarand` 和 `X-Uni-Crsf-Token` header |

skill 在 AI 分析过程中自动完成这套登录流程，用户无需感知。

## 5 步详解

### Step 1: 用户校验 → 拿 redirectURL

```
POST https://{ip}:31943/unisso/v2/validateUser.action
    ?service=/unisess/v1/auth?service=%2Fncecommonwebsite%2Fv1%2Fnewportal%2Fportal%2Floading%2Floading.html
Headers: {Accept: application/json, Content-Type: application/json;charset=UTF-8}
Body:    {"organizationName":"", "username":"xxx", "password":"xxx"}
```

**成功响应** (200):
```json
{"redirectURL": "/unisess/v1/auth?service=...&token=..."}
```

**失败信号**:
- 非 200 状态码 → 账号密码错（`ER_AUTH_FAILED`）
- `redirectURL` 为空 → 同上

### Step 2: 跟随重定向 → 拿 bspsession

```
GET https://{ip}:31943{redirectURL}
Headers: 同上
allow_redirects: False  ← 关键，手动跟随才能抓 cookie
```

**成功响应** (302): response cookies 里有 `bspsession`。

设 `headers["Cookie"] = "locale=zh-cn; bspsession={bsp}"`。

**失败信号**:
- `bspsession` 为空 → 登录被拒（多半还是账号问题，但 step 1 通过了，可能是会话冲突/锁定）

### Step 3: 跟随 license 页重定向

```
while status != 200:
    GET https://{ip}:31943{location_header}
    allow_redirects: False
```

跟随 302 重定向链直到 200。这步通常不会失败，只是建立会话。

### Step 4: 拿 csrfToken

```
GET https://{ip}:31943/unisess/v1/auth/session
Headers: 含 Cookie
allow_redirects: True
```

**成功响应** (200, JSON):
```json
{"csrfToken": "801da7512c8d4eddb7bf9ce08ade993f1bac45c2177c8224"}
```

设 `headers["roarand"] = csrf` 和 `headers["X-Uni-Crsf-Token"] = csrf`。

**失败信号**:
- 响应非 JSON → 环境异常
- `csrfToken` 为空 → session 接口异常，看 response body

### Step 5: NCE 登录跳转

```
GET https://{ip}:31943/plat/licapp/v1/licensedirectlogin?service=...{ip}%3A31943...index.html...
allow_redirects: False, 手动跟随重定向
```

部分接口（特别是 NCE 应用面门户相关）需要这步完成完整登录。healthcheck 等基础接口可能不需要，但走了也无害。

## 最终 header

```
Accept: application/json
Content-Type: application/json;charset=UTF-8
roarand: <csrfToken>
X-Uni-Crsf-Token: <csrfToken>
Cookie: locale=zh-cn; bspsession=<...>
```

## 关键约束

- **5 步必须用同一个 `requests.session()`** —— bspsession cookie 是 step 2 设到 session 上的，换 session 就丢 cookie
- **`verify=False`** —— NCE 自签证书
- **`urllib3.disable_warnings()`** —— 抑制 InsecureRequestWarning

## 排查流程

```
登录失败
  ├─ step 1 失败 (validateUser != 200) → 账号密码错
  ├─ step 2 失败 (bspsession 空)       → 账号问题 / 会话锁定
  ├─ step 4 失败 (csrfToken 空)        → 环境异常，看 session response body
  └─ step 5 卡住 (一直 302)             → license 服务异常
```

