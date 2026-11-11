# LinkKeeper
An AI Agent for managing interpersonal relationships, with memory, interaction tracking, rapport suggestions and Langfuse observability.

## 核心设计

| 机制 | 一句话 |
|---|---|
| 证据优先 | 文字/截图先入 evidence 拿稳定 ID，后续结论只能引用 ID |
| 写操作零直连 | 模型没有任何写工具，只能 `propose_action` 出一张卡片 |
| 三态分离 | `proposed`（提案）/ `confirm`（用户愿意）/ `receipt`（系统真写了）分别记录 |
| 幂等四件套 | requestId 唯一 + 回执唯一 + 行锁 + baseVersion CAS |
| 可恢复暂停 | 问卷落库后 run 暂停，回答后新建 run 恢复，不造假 toolCall |
| 分层记忆 | Evidence → 候选 → 用户确认 → 生效；长期文档按版本 + CAS |
| 洞察可追溯 | 回执后独立复盘，0~1 条，来源必须真实存在 |

## 快速开始

```bash
# 1. 依赖
pip install -e ".[dev]"

# 2. 配置
cp .env.example .env
#   填入 LLM_API_KEY；数据库用 compose 起或自备 PG

# 3. 起数据库
docker compose up -d --wait

# 4. 建表
python -m src.db.migrate

# 5. 起服务
python -m src.main
#   健康检查 http://127.0.0.1:8787/api/health
```

## 进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 基础地基：配置 / 数据访问层 / 19 个迁移 / 健康检查 | ✅ |
| P1 | 领域模型 + 自研 tool loop + 五维运行预算 | ✅ |
| P2 | 证据层 + MemorySnapshot 装配与渲染 | ✅ |
| P3 | 问卷可恢复暂停（配对补齐、失败可重提） | ✅ |
| P4 | Action Card：提案 / 确认 / 回执三态，CAS 抢占幂等 | ✅ |
| P5 | 记忆分层 + 人工审核 | 计划中 |
| P6 | 会话压缩 + Insight Agent | 计划中 |
| P7 | 40 条评测用例 + Langfuse | 计划中 |
| P8 | 单页前端 + 端到端 | 计划中 |

当前测试：`73 passed`

## 技术栈

Python 3.11+ · FastAPI · PostgreSQL · SQLAlchemy 2.0 Core（原生 SQL）· 自研 tool loop · Langfuse（可观测/语义评测）

## 目录

```
migrations/     19 个 SQL 迁移
src/            后端（agent / tools / memory / db / util）
evals/          40 条核心用例 + runner
web/            演示用单页
tests/          单元测试
```
