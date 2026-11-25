# Android Job Ledger Contract

## Purpose

为 Android 长任务提供持久化、可恢复、可观测的任务状态记录。Job ledger 只记录任务元数据和状态，不保存 provider secrets、模型密钥或完整对话内容。

## Record schema

每条记录至少包含：

| 字段 | 类型 | 约束 |
|---|---|---|
| `job_id` | string | 稳定、不可变、唯一 |
| `profile_id` | string | profile 隔离键，不允许跨 profile 查询 |
| `state` | enum | `queued`、`running`、`paused`、`succeeded`、`failed`、`cancelled` |
| `created_at` | integer | Unix milliseconds |
| `updated_at` | integer | 不早于 `created_at` |
| `started_at` | integer/null | 首次进入 `running` 时写入 |
| `finished_at` | integer/null | 终态时写入 |
| `retry_count` | integer | 从 0 开始，只能递增 |
| `last_error` | string/null | 脱敏后的短错误摘要，不含 secrets |
| `runtime_id` | string/null | 运行时 payload ID |
| `session_id` | string/null | Hermes 会话引用 |
| `notification_id` | string/null | Android 通知或恢复入口引用 |

## State transitions

允许的状态转移：

```text
queued   -> running | cancelled
running  -> paused | succeeded | failed | cancelled
paused   -> queued | running | cancelled
failed   -> queued | cancelled
succeeded -> (终态)
cancelled -> (终态)
```

非法转移必须被拒绝并保持原记录不变。重复提交同一个 `job_id` 必须幂等，不得产生第二条记录。

## Recovery rules

1. 应用或服务启动时扫描 `running` 记录。
2. 如果对应进程/任务心跳不存在，将记录转为 `paused`，并写入脱敏原因。
3. 恢复操作只能通过显式 `resume(job_id)` 进入 `queued` 或 `running`。
4. `retry_count` 超过策略上限后保持 `failed`，不能无限自动重试。
5. 升级或 runtime 切换不得删除 ledger；不兼容记录必须标记为 `failed` 并保留原因。
6. 查询和通知只返回当前 profile 的记录。

## Storage contract

- 建议使用 SQLite，数据库位于应用私有目录。
- schema 必须带版本号，并使用事务执行状态转移。
- 状态转移条件应包含旧状态，防止并发 worker 覆盖较新的状态。
- 数据库写入失败不能伪造成功通知；通知失败也不能回滚已经提交的任务状态。
- 日志和导出中不得包含 token、API key、Cookie、Authorization header 或完整用户输入。

## First implementation slice

1. 创建 SQLite schema 和迁移版本 `1`。
2. 实现 `create`、`get`、`list(profile_id)`、`transition`、`resume`、`markStale`。
3. 为非法转移、幂等创建、profile 隔离、并发旧状态保护增加测试。
4. 先接入 Hermes runtime 生命周期，再接入真正的 Agent job。
5. 在诊断页显示 ledger 中的任务数量和最近失败原因。

## Acceptance tests

- 进程被杀后，`running` 任务不会消失，而是变成 `paused` 或带原因的 `failed`。
- 锁屏和后台运行期间，任务状态可以通过通知和诊断页读回。
- 同一 `job_id` 重试不会生成重复任务。
- 不同 profile 之间无法读取或修改对方任务。
- 旧状态转移竞争时，较晚写入不能覆盖已发生的合法新状态。
- 数据库升级后已有任务和终态记录仍可读取。
