"""
数据库模块 —— 负责所有跟 PostgreSQL 打交道的事情
==============================================
包括：
- 创建/升级表结构
- 存储对话记录
- 存储/检索分层记忆
- 管理确认记录、摘要、未完事项
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

import asyncpg
import jieba

DATABASE_URL = os.getenv("DATABASE_URL", "")

# 搜索权重（向量搜索加入后可重新分配）
WEIGHT_KEYWORD = float(os.getenv("WEIGHT_KEYWORD", "0.5"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.3"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.2"))
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.1"))

ACTIVE_STATUS = "active"
MEMORY_TIER_EVERGREEN = "evergreen"
MEMORY_TIER_STABLE = "stable"
MEMORY_TIER_EPHEMERAL = "ephemeral"


# ============================================================
# 连接池管理
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未设置！")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        print("✅ 数据库连接池已创建")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("✅ 数据库连接池已关闭")


# ============================================================
# 表结构初始化
# ============================================================


async def init_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                model           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id              SERIAL PRIMARY KEY,
                content         TEXT NOT NULL,
                importance      INTEGER DEFAULT 5,
                source_session  TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_accessed   TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              CREATE TYPE memory_tier AS ENUM ('evergreen', 'stable', 'ephemeral');
            EXCEPTION
              WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              CREATE TYPE memory_status AS ENUM ('active', 'expired', 'conflicted', 'superseded');
            EXCEPTION
              WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )

        await conn.execute(
            """
            ALTER TABLE memories
              ADD COLUMN IF NOT EXISTS tier memory_tier NOT NULL DEFAULT 'ephemeral',
              ADD COLUMN IF NOT EXISTS status memory_status NOT NULL DEFAULT 'active',
              ADD COLUMN IF NOT EXISTS canonical_key TEXT,
              ADD COLUMN IF NOT EXISTS manual_locked BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS pending_review BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS replaced_by_id INTEGER,
              ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
            """
        )

        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'memories_replaced_by_id_fkey'
              ) THEN
                ALTER TABLE memories
                ADD CONSTRAINT memories_replaced_by_id_fkey
                FOREIGN KEY (replaced_by_id)
                REFERENCES memories(id)
                ON DELETE SET NULL;
              END IF;
            END
            $$;
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_confirmations (
                id            SERIAL PRIMARY KEY,
                memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                session_id    TEXT NOT NULL,
                confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (memory_id, session_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_summaries (
                id           SERIAL PRIMARY KEY,
                session_id   TEXT NOT NULL UNIQUE,
                summary      TEXT NOT NULL,
                mood         TEXT,
                topic_tags   TEXT[],
                msg_count    INTEGER,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_loops (
                id             SERIAL PRIMARY KEY,
                content        TEXT NOT NULL,
                loop_type      TEXT NOT NULL DEFAULT 'promise',
                source_session TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                resolved_at    TIMESTAMPTZ,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_fts
            ON memories
            USING gin(to_tsvector('simple', content));
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_session
            ON conversations (session_id, created_at);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_tier_active
            ON memories (tier, importance DESC, created_at DESC)
            WHERE status = 'active';
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_status
            ON memories (status);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_canonical
            ON memories (canonical_key)
            WHERE canonical_key IS NOT NULL;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_valid_until
            ON memories (valid_until)
            WHERE valid_until IS NOT NULL;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_pending_review
            ON memories (id)
            WHERE pending_review = TRUE;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_confirmations_memory
            ON memory_confirmations (memory_id);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_summaries_created
            ON session_summaries (created_at DESC);
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_loops_open
            ON open_loops (created_at DESC)
            WHERE status = 'open';
            """
        )

    print("✅ 数据库表结构已就绪")


# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9]*")
NUM_PATTERN = re.compile(r"\d{2,}")

_STOP_WORDS = frozenset(
    {
        "的",
        "了",
        "在",
        "是",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "这",
        "那",
        "有",
        "和",
        "与",
        "也",
        "都",
        "又",
        "就",
        "但",
        "而",
        "或",
        "到",
        "被",
        "把",
        "让",
        "从",
        "对",
        "为",
        "以",
        "及",
        "等",
        "个",
        "不",
        "没",
        "很",
        "太",
        "吗",
        "呢",
        "吧",
        "啊",
        "嗯",
        "哦",
        "哈",
        "呀",
        "嘛",
        "么",
        "啦",
        "哇",
        "喔",
        "会",
        "能",
        "要",
        "想",
        "去",
        "来",
        "说",
        "做",
        "看",
        "给",
        "上",
        "下",
        "里",
        "中",
        "大",
        "小",
        "多",
        "少",
        "好",
        "可以",
        "什么",
        "怎么",
        "如何",
        "哪里",
        "哪个",
        "为什么",
        "还是",
        "然后",
        "因为",
        "所以",
        "虽然",
        "但是",
        "已经",
        "一个",
        "一些",
        "一下",
        "一点",
        "一起",
        "一样",
        "比较",
        "应该",
        "可能",
        "如果",
        "这个",
        "那个",
        "自己",
        "知道",
        "觉得",
        "感觉",
        "时候",
        "现在",
    }
)


def extract_search_keywords(query: str) -> list[str]:
    keywords = set()

    for match in EN_WORD_PATTERN.finditer(query):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    for match in NUM_PATTERN.finditer(query):
        keywords.add(match.group())

    for word in jieba.cut(query, cut_all=False):
        word = word.strip()
        if not word:
            continue
        if EN_WORD_PATTERN.fullmatch(word) or NUM_PATTERN.fullmatch(word):
            continue
        if len(word) < 2 or word in _STOP_WORDS:
            continue
        keywords.add(word)

    return list(keywords)


# ============================================================
# 对话记录操作
# ============================================================


async def save_message(session_id: str, role: str, content: str, model: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model) VALUES ($1, $2, $3, $4)",
            session_id,
            role,
            content,
            model,
        )


async def get_recent_messages(session_id: str, limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            session_id,
            limit,
        )
        return list(reversed(rows))


async def get_session_messages(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            session_id,
        )


async def get_stale_unsummarized_sessions(idle_minutes: int = 30, limit: int = 5):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT c.session_id, MAX(c.created_at) AS last_message_at, COUNT(*)::int AS msg_count
            FROM conversations c
            LEFT JOIN session_summaries s ON s.session_id = c.session_id
            WHERE s.session_id IS NULL
            GROUP BY c.session_id
            HAVING MAX(c.created_at) < NOW() - make_interval(mins => $1)
            ORDER BY MAX(c.created_at) DESC
            LIMIT $2
            """,
            idle_minutes,
            limit,
        )


# ============================================================
# 记忆操作
# ============================================================


async def save_memory(
    content: str,
    importance: int = 5,
    source_session: str = "",
    tier: str = MEMORY_TIER_EPHEMERAL,
    status: str = ACTIVE_STATUS,
    canonical_key: Optional[str] = None,
    manual_locked: bool = False,
    pending_review: bool = False,
    replaced_by_id: Optional[int] = None,
    valid_until: Optional[datetime] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (
                content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review, replaced_by_id, valid_until
            )
            VALUES ($1, $2, $3, $4::memory_tier, $5::memory_status, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            content,
            importance,
            source_session,
            tier,
            status,
            canonical_key,
            manual_locked,
            pending_review,
            replaced_by_id,
            valid_until,
        )
        return row["id"] if row else None


async def get_memory(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, content, importance, source_session, tier, status, canonical_key,
                   manual_locked, pending_review, replaced_by_id, valid_until,
                   created_at, last_accessed
            FROM memories
            WHERE id = $1
            """,
            memory_id,
        )


async def touch_memories(memory_ids: Iterable[int]):
    ids = [mid for mid in memory_ids if mid]
    if not ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
            ids,
        )


async def search_memories(
    query: str,
    limit: int = 10,
    tiers: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
    created_after: Optional[datetime] = None,
    exclude_ids: Optional[Sequence[int]] = None,
    touch: bool = True,
):
    keywords = extract_search_keywords(query)
    if not keywords:
        return []

    params: list[object] = []
    case_parts: list[str] = []
    where_parts: list[str] = []
    for kw in keywords:
        params.append(kw)
        idx = len(params)
        case_parts.append(f"CASE WHEN content ILIKE '%' || ${idx} || '%' THEN 1 ELSE 0 END")
        where_parts.append(f"content ILIKE '%' || ${idx} || '%'")

    filters = [f"({' OR '.join(where_parts)})"]

    if statuses:
        params.append(list(statuses))
        filters.append(f"status::text = ANY(${len(params)}::text[])")
    if tiers:
        params.append(list(tiers))
        filters.append(f"tier::text = ANY(${len(params)}::text[])")
    if created_after:
        params.append(created_after)
        filters.append(f"created_at >= ${len(params)}")
    if exclude_ids:
        params.append(list(exclude_ids))
        filters.append(f"NOT (id = ANY(${len(params)}::int[]))")

    max_hits = len(keywords)
    hit_count_expr = " + ".join(case_parts)

    params.append(limit)
    sql = f"""
        SELECT
            id, content, importance, tier, status, pending_review,
            created_at, last_accessed,
            ({hit_count_expr}) AS hit_count,
            (
                {WEIGHT_KEYWORD} * ({hit_count_expr})::float / {max_hits}.0 +
                {WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                {WEIGHT_RECENCY} * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0))
            ) AS score
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY score DESC, importance DESC, created_at DESC
        LIMIT ${len(params)}
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        results = await conn.fetch(sql, *params)

    if MIN_SCORE_THRESHOLD > 0:
        results = [r for r in results if (r["score"] or 0) >= MIN_SCORE_THRESHOLD]

    if results and touch:
        await touch_memories([r["id"] for r in results])

    if results:
        print(
            f"🔍 搜索 '{query}' → 关键词 {keywords[:8]}{'...' if len(keywords) > 8 else ''} → 命中 {len(results)} 条"
        )
    else:
        print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]} → 无结果")
    return results


async def get_memories_by_tier(
    tier: str,
    limit: int = 20,
    days: Optional[int] = None,
    touch: bool = True,
):
    filters = ["tier::text = $1", "status::text = $2"]
    params: list[object] = [tier, ACTIVE_STATUS]
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        params.append(cutoff)
        filters.append(f"created_at >= ${len(params)}")
    params.append(limit)

    order_by = "importance DESC, created_at DESC" if tier == MEMORY_TIER_EVERGREEN else "created_at DESC, importance DESC"
    sql = f"""
        SELECT
            id, content, importance, source_session, tier, status,
            canonical_key, manual_locked, pending_review, replaced_by_id,
            valid_until, created_at, last_accessed
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY {order_by}
        LIMIT ${len(params)}
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    if rows and touch:
        await touch_memories([r["id"] for r in rows])
    return rows


async def get_recent_memories(limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                id, content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )


async def get_active_memory_briefs(limit: int = 50):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                LEFT(content, 80) AS brief,
                content,
                importance,
                tier,
                canonical_key,
                manual_locked,
                created_at
            FROM memories
            WHERE status = 'active'
            ORDER BY importance DESC, last_accessed DESC, created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return rows


async def expire_old_memories(ephemeral_days: int = 7):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET status = 'expired'
            WHERE tier = 'ephemeral'
              AND status = 'active'
              AND last_accessed < NOW() - make_interval(days => $1)
              AND manual_locked = FALSE
            """,
            ephemeral_days,
        )
        await conn.execute(
            """
            UPDATE memories
            SET status = 'expired'
            WHERE valid_until IS NOT NULL
              AND valid_until < NOW()
              AND status = 'active'
              AND manual_locked = FALSE
            """
        )


async def add_memory_confirmation(memory_id: int, session_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO memory_confirmations (memory_id, session_id)
            VALUES ($1, $2)
            ON CONFLICT (memory_id, session_id) DO NOTHING
            """,
            memory_id,
            session_id,
        )
    return result.endswith("1")


async def count_distinct_confirmations(memory_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT session_id)
            FROM memory_confirmations
            WHERE memory_id = $1
            """,
            memory_id,
        )
    return int(count or 0)


async def get_first_confirmation_time(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT MIN(confirmed_at) FROM memory_confirmations WHERE memory_id = $1",
            memory_id,
        )


async def upsert_session_summary(
    session_id: str,
    summary: str,
    mood: Optional[str] = None,
    topic_tags: Optional[Sequence[str]] = None,
    msg_count: Optional[int] = None,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO session_summaries (session_id, summary, mood, topic_tags, msg_count)
            VALUES ($1, $2, $3, $4::text[], $5)
            ON CONFLICT (session_id) DO UPDATE SET
              summary = EXCLUDED.summary,
              mood = EXCLUDED.mood,
              topic_tags = EXCLUDED.topic_tags,
              msg_count = EXCLUDED.msg_count,
              updated_at = NOW()
            """,
            session_id,
            summary,
            mood,
            list(topic_tags) if topic_tags else None,
            msg_count,
        )


async def has_session_summary(session_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM session_summaries WHERE session_id = $1",
            session_id,
        )
    return bool(exists)


async def get_recent_session_summaries(limit: int = 2):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, session_id, summary, mood, topic_tags, msg_count, created_at, updated_at
            FROM session_summaries
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )


async def get_latest_summary_time():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT MAX(created_at) FROM session_summaries")


async def create_open_loop(content: str, loop_type: str = "promise", source_session: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            """
            SELECT id
            FROM open_loops
            WHERE content = $1 AND status = 'open'
            LIMIT 1
            """,
            content,
        )
        if existing:
            return existing

        row = await conn.fetchrow(
            """
            INSERT INTO open_loops (content, loop_type, source_session)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            content,
            loop_type,
            source_session,
        )
    return row["id"] if row else None


async def get_open_loops(status: str = "open", limit: Optional[int] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if limit is None:
            return await conn.fetch(
                """
                SELECT id, content, loop_type, source_session, status, resolved_at, created_at
                FROM open_loops
                WHERE status = $1
                ORDER BY created_at DESC
                """,
                status,
            )
        return await conn.fetch(
            """
            SELECT id, content, loop_type, source_session, status, resolved_at, created_at
            FROM open_loops
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status,
            limit,
        )


async def resolve_open_loops(loop_ids: Sequence[int]):
    ids = [loop_id for loop_id in loop_ids if loop_id]
    if not ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE open_loops
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = ANY($1::int[]) AND status = 'open'
            """,
            ids,
        )


async def expire_old_open_loops(days: int = 14):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE open_loops
            SET status = 'expired'
            WHERE status = 'open'
              AND created_at < NOW() - make_interval(days => $1)
            """,
            days,
        )


async def get_all_memories_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def get_all_memories_detail():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id, content, importance, source_session, tier, status,
                canonical_key, manual_locked, pending_review,
                replaced_by_id, valid_until, created_at, last_accessed
            FROM memories
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def update_memory(
    memory_id: int,
    content: Optional[str] = None,
    importance: Optional[int] = None,
    tier: Optional[str] = None,
    status: Optional[str] = None,
    canonical_key: Optional[str] = None,
    manual_locked: Optional[bool] = None,
    pending_review: Optional[bool] = None,
    replaced_by_id: Optional[int] = None,
    valid_until: Optional[datetime] = None,
):
    updates = []
    params: list[object] = []

    def add(field_sql: str, value: object):
        params.append(value)
        updates.append(f"{field_sql} = ${len(params)}")

    if content is not None:
        add("content", content)
    if importance is not None:
        add("importance", importance)
    if tier is not None:
        add("tier", tier)
        updates[-1] += "::memory_tier"
    if status is not None:
        add("status", status)
        updates[-1] += "::memory_status"
    if canonical_key is not None:
        add("canonical_key", canonical_key)
    if manual_locked is not None:
        add("manual_locked", manual_locked)
    if pending_review is not None:
        add("pending_review", pending_review)
    if replaced_by_id is not None:
        add("replaced_by_id", replaced_by_id)
    if valid_until is not None:
        add("valid_until", valid_until)

    if not updates:
        return

    params.append(memory_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ${len(params)}",
            *params,
        )


async def delete_memory(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)


async def delete_memories_batch(memory_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = ANY($1::int[])", memory_ids)
