# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 / 结果数据集 | 任务编号、目标表名、目标大小、预计与实际收益 |

### 小文件合并的执行链路

1. **容量预估**：`GET /api/compact/estimate?table=lake.orders` 读取 `compact_shards` 里的现有分片统计，
   按目标大小顺序装箱，得到预计输出文件数与预计容量下降。示例表 `lake.orders` 有 240 个 6~18MB
   分片（`dt=2026-11-15/account_id=...` 两级目录），统计报告登记为 7.8GB。
2. **确认**：`POST /api/compact/confirm` 生成一份结果数据集（`CDS-*`）与一个执行批次（`BATCH-*`）。
   同一张表已有执行中的批次时返回 409，并发确认只保留一个批次；一次确认只对应一份合并结果。
3. **执行**：`POST /api/compact/batches/{批次}/run` 按并行度分段处理；统计异常的分片写为异常并留在
   剩余清单里，`POST /api/compact/batches/{批次}/resume` 从剩余分片继续。实际收益只在批次全部成功后
   记入该批次一次，重复执行返回 409，复核清单不会重复累计。
4. **复核**：`GET /api/compact/review` 每张表只保留一份快照——有成功批次展示实际收益，否则展示预计
   （分段）收益。`GET /api/compact/review/{数据集编号}` 回到复核详情，重复打开不会新增数据集记录。
5. **核对工作台**：`GET /api/compact/workbench` 按批次归组结果数据集、版本依据与复核清单；同表优化
   任务与运行中的批次冲突时给出唯一快照口径。
6. **统计报告**：`GET /api/compact/reports` 与工作台读同一批结果数据集，只按成功批次累计实际收益。
7. **版本依据**：`POST /api/compact/versions` 由其它业务模块（如表统计信息）把版本依据同步到工作台，
   同一模块对同一数据集只保留最新一条。
8. **导出与回看**：`GET /api/compact/export/file` 下载清单文件并留存；`GET /api/compact/exports`
   回看历史导出，`POST /api/compact/verify` 用下载文件逐批核对任务编号、数据集与收益。

前端对应页面：`/compact`（列表、容量预估、发起确认、复核详情入口）、`/compact/review/:datasetNo`
（复核详情）、`/compact/workbench`（合并收益核对工作台）。
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
