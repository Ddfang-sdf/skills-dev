# NCE swagger/yaml 接口定义文件格式速查

> AI 分析问题时需要调用 NCE/CloudSOP 微服务接口，接口定义通常在 swagger 2.0 格式的 yaml 文件里。本文档说明如何从 yaml 提取调用所需信息。

## 文件命名规律

NCE/CloudSOP 微服务的接口定义文件命名：`rest-{service-name}.yaml`

例：
- `rest-services-healthcheck.yaml` —— 服务健康检查
- `rest-event-query.yaml` —— 事件查询
- `rest-asset-query.yaml` —— 资产查询
- `rest-alarm.yaml` —— 告警

## 关键字段

### 顶层字段

```yaml
swagger: '2.0'
info:
  version: v1                    # 接口版本
  title: SecEyeCommonSituationService  # 服务名
basePath: /rest/seceyecommonsituationservice  # ← 关键：所有路径的基础前缀
schemes:
  - https                        # 协议，CloudSOP 都是 https
paths:                           # ← 关键：接口路径定义
  /v1/healthcheck:               # 路径（不含 basePath）
    get:                         # HTTP 方法
      ...
```

### 完整路径拼接

**完整路径 = basePath + path**

例：`basePath=/rest/seceyecommonsituationservice` + `path=/v1/healthcheck` → `/rest/seceyecommonsituationservice/v1/healthcheck`

### 接口定义结构

```yaml
paths:
  /v1/events/query:              # 路径
    post:                        # 方法 (get/post/put/delete/patch)
      summary: '查询事件列表'     # 接口描述
      operationId: EventQueryController  # 代码生成用，调用时忽略
      parameters:                # ← 输入参数
        - name: body             # 参数名
          in: body               # 位置: body/query/path/header
          required: true
          schema:
            $ref: '#/definitions/EventQueryRequest'  # 引用定义
      responses:                 # ← 响应
        200:
          description: '成功'
          schema:
            $ref: '#/definitions/EventQueryResponse'
        500:
          description: '服务异常'
```

## 从 yaml 提取调用信息的步骤

1. **读 basePath** —— 顶层 `basePath` 字段
2. **读 path** —— `paths` 下的 key
3. **读 method** —— path 下的 `get`/`post`/`put`/`delete`/`patch`
4. **拼完整路径** —— `basePath + path`
5. **构造 body**（仅 POST/PUT/PATCH）—— 看 `parameters` 里 `in: body` 的参数，按 `schema` 或 `$ref` 找到定义，构造对应的 JSON

## 参数位置（`in` 字段）

| `in` | 含义 | 调用时处理 |
|------|------|-----------|
| `body` | 请求体 | 构造 JSON，传给 `body` 字段 |
| `query` | URL 查询参数 | 拼到 path 后面 `?key=value` |
| `path` | 路径参数 | 替换 path 里的 `{placeholder}` |
| `header` | 请求头 | 加到 header dict |

## definitions（数据结构定义）

```yaml
definitions:
  EventQueryRequest:
    type: object
    properties:
      startTime:
        type: integer        # 长整型时间戳
        format: int64
      endTime:
        type: integer
        format: int64
      eventLevel:
        type: string         # 字符串，如 "2,3,4,5"
      eventType:
        type: string
      userType:
        type: array          # 数组
        items:
          type: string
```

构造 body 时按这些字段填值。类型不匹配（如把字符串填到整型字段）会被服务端拒绝。

## 实例：从 healthcheck yaml 提取调用

源文件 `rest-services-healthcheck.yaml`:
```yaml
basePath: /rest/seceyecommonsituationservice
paths:
  /v1/healthcheck:
    get:
      summary: '查询服务健康状态'
      responses:
        200:
          description: '返回状态码200表示服务健康'
```

提取结果：
- 完整路径：`/rest/seceyecommonsituationservice/v1/healthcheck`
- 方法：`GET`
- body：无（GET 无参数）
- 预期响应：200 = 健康

对应的 ER/IR task call：
```json
{"method": "GET", "path": "/rest/seceyecommonsituationservice/v1/healthcheck"}
```

## 实例：从带 body 的 yaml 提取

源文件（简化）：
```yaml
basePath: /rest/seceyecommonsituationservice
paths:
  /v1/event/management/count-query:
    post:
      parameters:
        - name: body
          in: body
          schema:
            $ref: '#/definitions/CountQueryRequest'
definitions:
  CountQueryRequest:
    properties:
      startTime: {type: integer}
      endTime: {type: integer}
      eventLevel: {type: string}
```

提取结果：
- 完整路径：`/rest/seceyecommonsituationservice/v1/event/management/count-query`
- 方法：`POST`
- body：`{"startTime": ..., "endTime": ..., "eventLevel": "..."}`

对应的 task call：
```json
{
  "method": "POST",
  "path": "/rest/seceyecommonsituationservice/v1/event/management/count-query",
  "body": {"startTime": 1783267200000, "endTime": 1785839254999, "eventLevel": "2,3,4,5"}
}
```

## 常见坑

1. **basePath 容易漏** —— 只看 `paths` 下的 key 会少一截路径，404
2. **`$ref` 要追踪** —— body schema 通常引用 definitions，要顺着 `$ref` 找到完整字段定义
3. **类型要对** —— `integer` 别传字符串，`array` 别传单值
4. **枚举值** —— 某些字段有 `enum` 约束，只能取枚举值
5. **必填字段** —— `required: true` 的字段必须填，否则 400
