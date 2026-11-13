# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

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
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 漏斗留存联合视图 | `funnel_retention` | 漏斗口径版本、群组快照与留存 | 口径版本、步骤转化率、第 1/7/30 天留存、异常标记 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 漏斗留存联合视图

漏斗是联合业务入口，转化窗口与留存观察周期共用同一口径版本，解决漏斗与同期群
各自独立定义、没有共同起算点的问题。

### 数据链路与快照结构

```text
启用漏斗（FunnelService.run_action）
  └─ JointRetentionService.on_funnel_enabled
       ├─ 冻结口径版本：hash(步骤事件序列 + 转化窗口 + 观察周期 + 检查点) → 版本号 漏斗编号@vN+hash
       ├─ 生成/复用同期群：划分事件=漏斗首步事件（共同起算点），窗口字段引用同一版本
       ├─ 按 joint_group_fact 群组原始事实逐群评估
       │    ├─ joint_snapshot   群组快照：步骤转化率、整体转化率、异常标记、事件/窗口来源
       │    └─ joint_retention  留存行：检查点（默认 1/7/30 天）留存率与状态原因
       └─ joint_audit           审计：动作、幂等键、事件来源、窗口来源、关联快照 id
```

- **同版本去重与并发**：进程锁 + 幂等键 `generate:漏斗:版本` + 版本去重，同一漏斗
  并发生成、重复提交同版本新建结果至多一份。
- **换版续算边界**：步骤事件、转化窗口、观察周期、检查点任一变化即出新版本；
  旧快照/旧留存永不覆盖。群组依赖的事件/窗口/周期变化才重算，未受影响群组生成
  「续算沿用」快照（`重算=false`、`来源快照id` 指回旧行），绝不冒充重算结果。
- **异常标记（单元格置空，不沿用上一个完整结果）**：
  - 群组粒度为空：群组标记，转化率与留存率全部置空；
  - 缺少中间步骤：缺失步骤及其下游转化率置空；
  - 观察周期越界：超出观察周期的检查点留存置空并写明原因；
  - 样本不足：层级保留，数值标注「仅供参考」。
- 联合视图、漏斗详情（`活跃版本`）、同期群状态（`口径版本`）三处引用完全一致的版本；
  审计记录可看到每次生成/换版的事件来源与窗口来源。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/funnel-retention` | 联合视图列表，支持 `funnel_code` 过滤 |
| GET | `/api/funnel-retention/funnels/{id}` | 单个漏斗的联合视图（含全部历史版本） |
| GET | `/api/funnel-retention/snapshots/{id}` | 群组快照明细（含留存行与来源） |
| POST | `/api/funnel-retention/funnels/{id}/generate` | 手动生成（同版本幂等去重） |
| POST | `/api/funnel-retention/funnels/{id}/revise` | 步骤换版（旧结果保留、受影响群组重算） |
| GET | `/api/funnel-retention/funnels/{id}/audits` | 审计记录 |

### 验证脚本

```bash
# 服务层：快照结构、异常标记、换版续算、幂等并发（16 项断言）
backend/.venv/bin/python -I scripts/verify_joint_retention.py
# HTTP 端到端：需先启动后端
backend/.venv/bin/python -I scripts/verify_http.py
```

领域：数据工程
子方向：数据分析与数据应用
