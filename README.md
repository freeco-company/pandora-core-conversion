# pandora-core-conversion (py-service)

Pandora 集團轉換漏斗服務（loyalist → franchisee）。實作 [ADR-003 v1.1](../docs/adr/ADR-003-loyalist-to-franchisee-conversion.md)。

> **Status**：v0.1 skeleton — Phase A 落地用骨架。core schema、5 個埋點 endpoints、JWT verifier、lifecycle state machine 都 wired 了，但複雜業務規則（engaged / loyalist 判定、fairysalebox 對接、漏斗 dashboard）標 TODO 留後續 PR。

---

## 角色

集團 platform 一級系統，**不屬於任何 App**。各 App（豆豆 / 月曆 / 肌膚 / 學院 / 母艦）透過 Pandora Core JWT 認證後上報事件，本服務維護使用者 lifecycle 狀態 + 加盟訓練進度 + 加盟申請流程。

```
App (doudou / pandora_js_store / fairy_*) 
   │  Authorization: Bearer <Pandora Core JWT>
   ▼
py-service (FastAPI + PostgreSQL 16)
   ├─ conversion_events (PARTITIONED RANGE occurred_at)
   ├─ lifecycle_transitions
   ├─ franchise_training_progress
   └─ franchise_applications
```

JWT 走 [pandora-core-identity](https://github.com/freeco-company/pandora-core-identity) 發的 RS256（公鑰從 `/api/v1/auth/public-key` 拉，cache 1h）。

---

## Tech stack

- Python 3.12+
- FastAPI + Uvicorn
- SQLAlchemy 2.x async + asyncpg
- Alembic
- PostgreSQL 16
- pytest / pytest-asyncio + aiosqlite (test only)
- ruff + mypy
- python-jose[cryptography] (RS256 verify)

---

## Local dev

### 1. Postgres

```bash
docker compose up -d postgres
```

或自己跑一個 Postgres 16，DB name `pandora_conversion`，user / pass `pandora` / `pandora`（或改 `.env`）。

### 2. Python deps

```bash
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

### 3. Migration

```bash
alembic upgrade head
```

⚠️ Migration 用了 PostgreSQL 專屬 partitioned table 語法（`PARTITION BY RANGE`）— **只能跑在 Postgres，不能跑在 sqlite**。Tests 直接靠 `Base.metadata.create_all` 建表（見 `tests/conftest.py`）。

### 4. Run

```bash
uvicorn app.main:app --reload --port 8002
curl localhost:8002/health
```

### 5. Tests

```bash
pytest -v
ruff check .
mypy app
```

---

## API（v1）

全部需 `Authorization: Bearer <JWT>`，token 由 [pandora-core-identity](https://github.com/freeco-company/pandora-core-identity) 簽發。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | 健康檢查（無認證）|
| `POST` | `/api/v1/events` | 通用事件 ingest（事件主體＝token sub）|
| `GET` | `/api/v1/users/{uuid}/lifecycle` | 查 lifecycle status + 歷史 |
| `POST` | `/api/v1/users/{uuid}/lifecycle/transition` | Admin / 內部觸發 transition（需 `lifecycle:write` scope）|
| `GET` | `/api/v1/users/{uuid}/training` | 查訓練章節進度 |
| `POST` | `/api/v1/users/{uuid}/training` | 更新訓練章節進度 |

ADR-003 §2.3 最小事件集：`app.opened` / `engagement.deep` / `franchise.cta_view` / `franchise.cta_click` / `academy.training_progress`。其他 event_type 也接，落到 `conversion_events` 給 analytics 用。

### 範例：上報 `app.opened`

```bash
curl -X POST localhost:8002/api/v1/events \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "app_id": "doudou",
    "event_type": "app.opened",
    "payload": {"version": "1.0.0"},
    "occurred_at": "2026-04-28T12:00:00Z"
  }'
```

---

## Lifecycle state machine

ADR-003 §2.2：

```
visitor → registered → engaged → loyalist → applicant → franchisee
```

| Transition | 規則 | v1 實作？ |
|---|---|---|
| `visitor → registered` | 該 uuid 第一個 `app.opened` 事件 | ✅ |
| `registered → engaged` | 累積互動 ≥ 60 天 OR 訂閱事件 | ⏸ TODO |
| `engaged → loyalist` | 連續 3 個月活躍 + 母艦復購 ≥ 2 次 | ⏸ TODO |
| `loyalist → applicant` | `franchise.cta_click` 事件 | ⏸ TODO |
| `applicant → franchisee` | 訓練全通過 + 首單付款 | ⏸ Manual via `/lifecycle/transition` |

擴展方式：在 `app/conversion/lifecycle.py` 新增 `async def rule_*(ctx) -> str | None`，加進 `DEFAULT_RULES`。

---

## Skeleton 完成度

✅ Done
- FastAPI app + lifespan + health
- Async SQLAlchemy + asyncpg
- 4 個 core tables + Alembic migration（含 Postgres partitioning）
- Pandora Core RS256 JWT verifier（cache 1h、issuer / scopes / product whitelist）
- 5 個埋點 endpoints（含 auth + scope check）
- Lifecycle state machine 骨架 + 第一條規則 (`visitor → registered`)
- Service 層 + Pydantic schemas
- 9 tests（health 1 + ingest 3 + lifecycle 5）綠
- GitHub Actions CI（pytest with PG service container + ruff + mypy）
- Docker Compose（postgres + app）

⏸ TODO（後續 PR）
- ADR-003 §7.1 各 App `LoyalistRule` interface
- `engaged` / `loyalist` 判定（需跨 App 聚合 + 母艦訂單 join）
- `franchise_applications` ingest 流程（學院考核通過 → 寫 application）
- fairysalebox webhook 對接（ADR-003 §2.5，CEO 指示低優先）
- 漏斗 dashboard endpoints (`/funnel/metrics`)
- Internal-secret middleware（給 server-to-server 非用戶 JWT 場景）
- Partition 自動化（monthly partition cron）
- 部署（不在本 PR 範圍）

---

## 不做（v1）

- 不接 fairysalebox webhook（ADR-003 §2.5，等母艦整合 epic）
- 不做完整 lifecycle 規則邏輯
- 不做客戶端 SDK（豆豆 / 母艦 / 學院的接入層留後續）
- 不部署、不上 prod

---

## 相關文件

- [ADR-003 愛用者→加盟者轉換](../docs/adr/ADR-003-loyalist-to-franchisee-conversion.md) — 設計依據（v1.1 Accepted）
- [ADR-007 Identity 同步策略修訂](../docs/adr/ADR-007-identity-sync-strategy-revision.md) — JWT 來源 platform
- [集團 CLAUDE.md](../CLAUDE.md) — 集團憲法
- [pandora-core-identity](https://github.com/freeco-company/pandora-core-identity) — JWT 簽發方
