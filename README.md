# 家具工艺巡查资料服务

登记家具生产工艺、污染要素、企业标签和责任人员，并在此基础上提供**按工艺与风险差异下发的专属巡查清单**：业务人员起草模板版本、选择适用工艺与风险条件、经复核后按生效区间发布；企业级增删项作为有理由、有期限的覆盖层；每日生成巡查任务时冻结最终条目与来源，历史任务不被后续发布、回滚或标签变化改写。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。

## 领域资料

领域资料类别为：`process_profile`（工艺）、`pollution_factor`（污染要素）、`enterprise_tag`（企业标签）、`reviewer_assignment`。

清单条目通过 `applicable_processes`（命中工艺 external_key）与 `risk_conditions`（命中污染要素/标签 external_key）声明适用性；两者均空表示对所有企业适用。

## 清单版本生命周期

```
draft ──submit──▶ pending_review ──review(approved)──▶ publish(生效区间) ──▶ published
  ▲                    │
  └── review(rejected) ┘                                   published ──rollback──▶ rolled_back
```

- 一个组织只有一个模板，版本号单调递增；草稿可反复修改，提交后进入待复核。
- 四眼原则：提交人不能复核自己的版本；复核通过后才能发布。
- 发布带 `[effective_from, effective_until)` 生效区间：与既有已发布区间重叠会被确定拒绝；新版本接续开放版本时自动把旧版本收口；禁止追溯发布。
- 重复发布、重复提交、重复复核、重复回滚都返回业务冲突，状态不被改写。
- 回滚只影响回滚之后**新生成**的任务；回滚造成的空档由更早的已发布版本延续适用（来源中标记 `fallback_after_rollback`）。

## 企业覆盖层

- 按场所创建，必须填写 `reason`、`effective_from`、`expires_at`，变更类型为 `add` / `remove` / `modify`。
- 多个覆盖层按 `(effective_from, created_at, overlay_id)` 确定叠加顺序，后生效者覆盖先生效者；每个企业场所的修订号单调递增。
- 覆盖层可撤销（同样需要理由）；撤销与到期只影响尚未生成的任务。

## 每日任务冻结

- 每天为场所生成一个巡查任务，生成时把**最终条目、每条来源链、企业工艺/风险标签快照、生效覆盖层快照**整体哈希（`manifest_hash`）冻结入库。
- 任务开始后，后续的版本发布、回滚、覆盖层撤销/到期、企业标签变化都不会改写它。
- 已开始/已完成任务的重复开始、完成调用返回确定结果。

## 并发与查询

- 所有写事务以 `BEGIN IMMEDIATE` 串行提交，配合应用级锁，并发提交、并发复核、并发任务生成由唯一成功者胜出，其余得到确定的业务冲突或幂等重放。
- 查询接口同时回答：
  - `GET /checklist/current`：企业当前（或任意日期）适用的最终清单与来源；
  - `GET /checklist/future-changes`：未来的版本切换、覆盖层生效与到期；
  - `GET /inspection-tasks/{id}` 与 `/rule-sources`：任意历史任务冻结的条目、版本、覆盖层与标签快照来源。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/checklist-templates` | 为组织创建唯一模板 |
| POST/GET | `/checklist-versions` | 起草版本 / 列出版本 |
| GET | `/checklist-versions/{id}/detail` | 版本详情（含条目） |
| POST | `/checklist-versions/{id}/update` | 修改草稿 |
| POST | `/checklist-versions/{id}/submit` | 提交复核 |
| POST | `/checklist-versions/{id}/review` | 复核（approved/rejected，四眼原则） |
| POST | `/checklist-versions/{id}/publish` | 按生效区间发布 |
| POST | `/checklist-versions/{id}/rollback` | 回滚已发布版本 |
| POST/GET | `/checklist-overlays` | 创建企业覆盖层 / 列出 |
| POST | `/checklist-overlays/{id}/revoke` | 撤销覆盖层 |
| GET | `/checklist/current` | 当前/任意日期清单 |
| GET | `/checklist/future-changes` | 未来变更 |
| POST/GET | `/inspection-tasks` | 生成每日任务 / 列出 |
| POST | `/inspection-tasks/{id}/start`、`/complete` | 开始、完成任务 |
| GET | `/inspection-tasks/{id}`、`/rule-sources` | 历史任务冻结快照与规则来源 |

所有写操作都在事务中完成，同一 `request_id` 携带相同内容返回原结果（`replayed=true`），内容变化时返回业务冲突。写接口通过 `X-Actor-Id` 标识操作者。

## 目录

- `src/inspection_catalog_core/`：领域模型、SQLite 存储、基础资料服务、清单模型、清单解析引擎、清单服务、审计链、HTTP 路由和离线验收；
- `tests/`：解析引擎、清单规则、事务并发、接口路由和端到端验收测试。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m inspection_catalog_core.acceptance
```

命令会在临时 SQLite 数据库中登记操作者、场所、工艺与风险标签，起草、复核并发布差异化清单，叠加企业覆盖层，生成并开始每日任务，再发布新版本核对冻结结果，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m inspection_catalog_core.api --database inspection_catalog_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。服务重启后，SQLite 中的业务状态、冻结任务和审计链继续保留。
