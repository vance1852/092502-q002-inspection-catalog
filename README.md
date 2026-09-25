# 家具工艺巡查资料服务

登记家具生产工艺、污染要素、企业标签和责任人员，并按企业工艺差异管理**专属巡查清单**：
业务人员起草模板版本、选择适用工艺与风险条件，经复核后按生效区间发布；
企业级增删项作为有理由、有期限的覆盖层；每日巡查任务在生成时冻结最终条目与规则来源，
此后版本发布、撤销或企业标签变化都不会改写历史任务。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、
场所台账、领域资料记录、请求幂等校验、哈希串联审计、清单版本生命周期、企业覆盖层、
任务快照冻结以及轻量 HTTP/JSON 接口。领域资料类别为：process_profile、pollution_factor、
enterprise_tag、reviewer_assignment。所有写操作都在 `BEGIN IMMEDIATE` 短事务中完成，
同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 清单服务的关键规则

### 模板版本生命周期

`draft → submitted → approved/rejected → published → revoked`

* 业务人员（operator/admin）起草和修改**仅草稿**版本，条目编码在版本内不可重复；
* 提交后进入待复核；**提交人不能复核自己的版本**（含 admin），由 reviewer/admin 复核；
* 复核驳回需填写理由，被驳回版本不可再改，需重新起草；
* 只有复核通过的版本可以发布，发布必须给出半开生效区间 `[effective_from, effective_to)`；
* **同一模板下已发布版本的生效区间不允许重叠**，端点相接（相邻换版）允许；重复发布返回冲突，
  使用原 `request_id` 重放则返回原回执；
* 撤销立即生效，**只影响撤销之后生成的任务**；已冻结任务不做任何修改。

### 适用工艺与风险条件

版本适用性由 `process_profiles`（必填、非空）与 `enterprise_tags`（可为空表示不限风险）构成。
企业当前工艺/标签取自其 `domain_records`，两个维度各自取交集、维度之间为“与”。
企业标签变化的确定处理是：当前清单按当前标签实时解析；历史任务在生成时已冻结标签集合与条目，
不受后续变化影响。

### 企业覆盖层

* `add` 覆盖必须提供条目内容与理由，`remove` 覆盖必须提供理由且不能携带内容；
* 每条覆盖都有半开有效期，**最长 366 天**；同企业、同种类、同条目编码、同模板作用域的
  有效覆盖区间不允许重叠（相邻允许）；
* 覆盖可提前撤销，撤销同样只影响此后生成的任务；
* 多个模板同时适用时，条目按模板编号/版本号排序合并，同编码以排序靠后者为准，
  两个版本都保留在来源链上；覆盖在基线之后应用，未限定模板的覆盖作用于全部适用模板。

### 每日任务冻结

生成每日任务时，系统解析当时生效的全部版本与企业覆盖，把**最终条目逐行写入任务快照**，
每行带来源（模板版本或企业覆盖，含编码、编号与版本号），并对解析上下文与全部条目计算
`snapshot_hash`。任务一经生成：

* 同企业同一天重复创建是确定操作，直接回放原任务，绝不重新解析；
* 开始后的任务不能被后来发布、撤销（回滚）、覆盖到期或标签变化改写；
* 任务状态机：`generated → started → completed`，仅未开始任务可取消。

### 查询接口同时回答三件事

* `GET /checklist/current`：某企业在指定时刻适用的当前清单（版本、覆盖、删除痕迹、条目来源）；
* `GET /checklist/future`：相对当前时刻的未来变更事件（版本生效/到期、覆盖开始/到期），按时间排序；
* `GET /task`：任意历史任务的冻结条目与逐条规则来源（版本 ID、覆盖 ID、解析上下文、快照哈希）。

### 并发的确定处理

所有状态推进使用 `BEGIN IMMEDIATE` 事务 + 条件 UPDATE 状态守卫（`WHERE status=...`）：
两个复核员同时审批、两个重叠区间同时发布时，恰好一方成功，另一方得到 409 冲突，
不会出现双重审批或重叠生效。

## HTTP 接口

基础资料：

* `POST /organizations`、`POST /actors`、`POST /sites`、`POST /domain-records`
* `GET /domain-records?site_id=...[&category=...]`、`GET /audit-events`

清单模板与版本：

* `POST /checklist-templates`、`GET /checklist-templates`
* `POST /checklist-versions`（起草）、`POST /checklist-versions/update`（改草稿）
* `POST /checklist-versions/submit`、`POST /checklist-versions/review`
* `POST /checklist-versions/publish`、`POST /checklist-versions/revoke`
* `GET /checklist-versions?template_id=...`、`GET /checklist-version?version_id=...`

企业覆盖层：

* `POST /overrides`、`POST /overrides/revoke`、`GET /overrides?site_id=...`

每日任务与查询：

* `POST /daily-tasks`、`POST /tasks/start`、`POST /tasks/complete`、`POST /tasks/cancel`
* `GET /tasks?site_id=...`、`GET /task?task_id=...`
* `GET /checklist/current?site_id=...[&effective_at=...]`
* `GET /checklist/future?site_id=...[&within_to=...]`

时间参数接受 `YYYY-MM-DD`（按 UTC）或带时区的 ISO-8601；存储统一为 UTC `Z` 字符串，
半开区间比较直接使用字典序。

## 目录

- `src/inspection_catalog_core/`：领域模型、SQLite 存储、基础资料服务、清单解析引擎、
  清单服务、审计链、HTTP 路由和离线验收；
- `tests/`：解析规则、服务生命周期、事务边界与并发审批、接口路由和端到端验收测试。

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

命令会在临时 SQLite 数据库中登记操作者、场所、工艺与标签，演练模板起草、四眼复核、
区间发布、企业覆盖、每日任务冻结与撤销后历史不变，成功时输出一行 `status` 为 `ok` 的
JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m inspection_catalog_core.api --database inspection_catalog_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者。服务重启后，
SQLite 中的业务状态、审计链与任务快照继续保留；对既有数据库会自动补齐后加的任务列。
