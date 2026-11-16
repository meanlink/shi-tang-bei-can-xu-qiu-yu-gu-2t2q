# 食堂备餐需求预估

季节需求预测的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 厨房少备对账（按档口登记采用建议、实领批次逐日对账、缺日待补齐、连续三天超量预警）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；厨房追踪新增档口账本、采用方案、实领批次、预警与沿革、幂等操作记录；辅助 User、Audit、Delivery。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18213；后端文档 http://localhost:19213/docs；数据库宿主端口 17713。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、厨房登记与预警确认/撤销、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、厨房登记（采用建议与实领批次）、读取与下载；不能批准、确认预警或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 厨房连续少备追踪
页面「厨房少备追踪」按档口维护逐日账本：

- **登记**：从已完成且获批准的分析中选日期登记实际采用的建议（与建议不同须填原因）；实领按批次号登记，同日可多批，批次号重复视为更正并生成新版本。
- **对账**：逐日列出建议、采用、实领、偏差（实领−建议）与偏差（实领−采用）；缺采用建议、缺实领批次或整日缺失都进入「待补齐」，不参与连续超量判断。
- **预警**：同一档口连续 3 个已完整对账日实领均超过采用建议，开一条预警；同一连续超量段只保留一条活动预警。
- **更正重算**：补传或更正后重算受影响区间；区间合并或拆分时旧预警归档并保留前驱/后继关系。未处理预警可撤销；已确认预警保留确认时的依据快照，后续更正只追加说明。确认请求必须携带当前依据校验值，依据已被更正时拒绝旧依据确认。
- **中断恢复**：每次写操作带幂等键，worker 会重放中断的 pending 操作；同一操作编号重试不会重复开预警或重复追加更正说明。页面「中断恢复」可手动重放。
- **追溯**：页面可查采用方案与实领批次的版本链、预警沿革、确认/撤销/更正记录及每条的幂等操作编号。

接口在 `/kitchen` 下：`stalls`、`ledger`、`pending`、`alerts`、`alerts/{id}`、`trail`、`adoptions`、`batches`、`alerts/{id}/confirm`、`alerts/{id}/revoke`、`recalculate`、`operations/{operation_id}`、`operations/recover`。演示 seed 预置一个档口：11-19 缺实领批次、11-20 缺采用建议，KQ-000001 已确认后有更正说明，KQ-000003/4 合并为 KQ-000005。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=meal-seasonal；FRONTEND_PORT=18213；BACKEND_PORT=19213；DB_PORT=17713；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1014/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
