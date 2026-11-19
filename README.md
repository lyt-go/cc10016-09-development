# 多源数据采集编排平台

面向数据源接入、采集编排、内容解析、反爬对抗与数据校验的全链路数据采集平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集源管理 | `crawl_source` | 采集源 | 源标识、源名称、协议类型 |
| 种子管理 | `seed_config` | 采集种子 | 种子编号、目标URL模式、列表页XPath |
| 解析规则 | `parse_rule` | 解析规则 | 规则编号、目标字段名、选择器类型 |
| 频次控制 | `rate_limit` | 限流策略 | 策略编号、目标源、请求间隔 |
| 代理池调度 | `proxy_pool` | 代理节点 | 节点编号、代理地址、端口号 |
| 会话管理 | `session_mgr` | 采集会话 | 会话编号、关联源、登录状态 |
| 重试策略 | `retry_policy` | 重试策略 | 策略编号、目标源、最大重试次数 |
| 字段抽取 | `field_extract` | 抽取模板 | 模板编号、关联种子、字段名 |
| 反爬对抗 | `anti_spider` | 对抗策略 | 策略编号、目标源、反爬类型 |
| 渲染编排 | `page_render` | 渲染配置 | 配置编号、目标源、渲染引擎 |
| 数据校验 | `data_validate` | 校验规则 | 规则编号、目标字段、校验函数 |
| 增量游标 | `incre_cursor` | 增量标记 | 标记编号、关联种子、增量字段 |
| 去重过滤 | `dedup_filter` | 去重规则 | 规则编号、目标源、去重键列 |
| 格式转换 | `format_convert` | 转换配置 | 配置编号、源格式、目标格式 |
| 采集编排 | `crawl_schedule` | 采集计划 | 计划编号、关联种子、Cron表达式 |
| 结果落地 | `result_store` | 落地配置 | 配置编号、输出格式、存储路径 |
| 采集监控 | `crawl_monitor` | 监控指标 | 指标编号、关联源、成功率 |
| 内容抽取 | `content_extract` | 提取任务 | 任务编号、来源页面、内容类型 |

## 代理调度：会话地域归属

代理池调度不再只看全局健康评分，而是先按会话归属编排：

- **归属来源**：会话显式登记的 `归属地域`，否则取关联采集源的 `地区归属`；
  「全国」源可在境内地域就近调度，但不会落到海外节点。
- **双条件**：代理地域必须落在会话归属范围内，且健康状态可用（状态为「可用」、
  成功率/延迟齐全、健康评分不低于阈值）。任一缺失都不分配。
- **本域优先**：归属范围内存在健康可用代理时按评分取最高，不用跨域代理。
- **跨域**：`cross_region=true` 时必须带 `授权编号 / 授权人 / 授权范围`，授权范围要覆盖
  目标地域；调度结果会记录授权依据、原会话地域与越域原因。未授权跨域不写结果。
- **版本仲裁**：每次调度结果带递增 `version`。提交方可以带 `expected_version`，
  落后于当前版本时返回 `version_conflict` 并记录仲裁日志，已有结果不被覆盖。
- **同源与只读**：调度结果只由 `proxy_pool` 写入（`proxy_dispatch_results`），
  会话管理、采集编排等模块只读引用；越权写入返回 `forbidden_writer`。
- **请求日志**：会话请求记录只能由会话管理写入，且必须引用调度结果中的代理编号；
  未分配或跨域无授权的代理不会写进请求日志。同一会话只保留一个有效代理绑定。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/proxy_pool/dispatch` | 生成/更新会话代理调度结果（跨域需授权，支持 `expected_version`） |
| GET | `/api/proxy_pool/dispatch` | 只读列出调度结果 |
| GET | `/api/proxy_pool/dispatch/conflicts` | 版本冲突仲裁记录 |
| POST | `/api/session_mgr/{id}/dispatch` | 会话侧申请调度 |
| GET | `/api/session_mgr/{id}/dispatch` | 只读查看该会话调度结果 |
| POST | `/api/session_mgr/{id}/dispatch/write` | 受控写入入口，仅 `proxy_pool` 可写 |
| POST | `/api/session_mgr/{id}/record` | 写请求日志（含代理编号与归属依据） |

验收脚本：`cd backend && .venv/bin/python scripts/proxy_dispatch_acceptance.py`
演示华东会话在本域低分代理与跨域高分代理之间的取舍、版本冲突与越权拒绝。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
