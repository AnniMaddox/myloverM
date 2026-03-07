"""
AI Memory Gateway — 带记忆系统的 LLM 转发网关
=============================================
让你的 AI 拥有长期记忆。

工作原理：
1. 接收客户端（Kelivo / ChatBox / 任何 OpenAI 兼容客户端）的消息
2. 自动搜索数据库中的相关记忆，注入 system prompt
3. 转发给 LLM API（支持 OpenRouter / OpenAI / 任何兼容接口）
4. 后台自动存储对话 + 用 AI 提取新记忆

环境变量 MEMORY_ENABLED=false 时退化为纯转发网关（第一阶段）。
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from database import (
    ACTIVE_STATUS,
    MEMORY_TIER_EPHEMERAL,
    MEMORY_TIER_EVERGREEN,
    MEMORY_TIER_STABLE,
    add_memory_confirmation,
    close_pool,
    count_distinct_confirmations,
    create_open_loop,
    delete_memories_batch,
    delete_memory,
    expire_old_memories,
    expire_old_open_loops,
    get_active_memory_briefs,
    get_all_memories,
    get_all_memories_count,
    get_all_memories_detail,
    get_first_confirmation_time,
    get_latest_summary_time,
    get_memories_by_tier,
    get_memory,
    get_open_loops,
    get_pool,
    get_recent_session_summaries,
    get_session_messages,
    get_stale_unsummarized_sessions,
    init_tables,
    resolve_open_loops,
    save_memory,
    save_message,
    search_memories,
    update_memory,
    upsert_session_summary,
)
from memory_extractor import extract_memory_actions, score_memories, summarize_session

# ============================================================
# 配置项 —— 全部从环境变量读取，部署时在云平台面板里设置
# ============================================================

# 你的 API Key（OpenRouter / OpenAI / 其他兼容服务）
API_KEY = os.getenv("API_KEY", "")

# API 地址（改这个就能切换不同的 LLM 服务商）
# OpenRouter: https://openrouter.ai/api/v1/chat/completions
# OpenAI:     https://api.openai.com/v1/chat/completions
# 本地 Ollama: http://localhost:11434/v1/chat/completions
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 默认模型（如果客户端没指定就用这个）
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "anthropic/claude-sonnet-4")

# 网关端口
PORT = int(os.getenv("PORT", "8080"))

# 记忆系统开关（数据库出问题时可以临时关掉）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "false").lower() == "true"

# 每次注入的最大记忆条数
MAX_MEMORIES_INJECT = int(os.getenv("MAX_MEMORIES_INJECT", "15"))
MAX_EVERGREEN_INJECT = int(os.getenv("MAX_EVERGREEN_INJECT", "12"))
MAX_STABLE_INJECT = int(os.getenv("MAX_STABLE_INJECT", str(MAX_MEMORIES_INJECT)))
MAX_EPHEMERAL_INJECT = int(os.getenv("MAX_EPHEMERAL_INJECT", "8"))
MAX_SUMMARIES_INJECT = int(os.getenv("MAX_SUMMARIES_INJECT", "2"))
MAX_OPEN_LOOPS_INJECT = int(os.getenv("MAX_OPEN_LOOPS_INJECT", "8"))

# 记忆提取间隔（0 = 禁用自动提取，1 = 每轮提取，N = 每 N 轮提取一次）
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "1"))

# 时区偏移（小时），用于记忆注入时的日期显示，默认 UTC+8
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "30"))
MIN_MESSAGES_FOR_SUMMARY = int(os.getenv("MIN_MESSAGES_FOR_SUMMARY", "4"))
EPHEMERAL_CONFIRMATIONS_TO_STABLE = int(os.getenv("EPHEMERAL_CONFIRMATIONS_TO_STABLE", "3"))
STABLE_CONFIRMATIONS_TO_REVIEW = int(os.getenv("STABLE_CONFIRMATIONS_TO_REVIEW", "5"))
STABLE_REVIEW_DAYS = int(os.getenv("STABLE_REVIEW_DAYS", "14"))

# 轮次计数器
_round_counter = 0

# 额外的请求头（有些 API 需要，比如 OpenRouter 需要 Referer）
EXTRA_REFERER = os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local")
EXTRA_TITLE = os.getenv("EXTRA_TITLE", "AI Memory Gateway")

META_BLACKLIST = [
    "记忆库",
    "记忆系统",
    "检索",
    "没有被记录",
    "没有被提取",
    "记忆遗漏",
    "尚未被记录",
    "写入不完整",
    "检索功能",
    "系统没有返回",
    "关键词匹配",
    "语义匹配",
    "语义检索",
    "阈值",
    "数据库",
    "seed",
    "导入",
    "部署",
    "bug",
    "debug",
    "端口",
    "网关",
]
SKIPPED_SUMMARY_PREFIX = "【短会话略过】"


# ============================================================
# 人设加载
# ============================================================

def load_system_prompt():
    """从 system_prompt.txt 文件读取人设内容"""
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except FileNotFoundError:
        pass
    print("ℹ️  未找到 system_prompt.txt 或文件为空，将不注入 system prompt")
    return ""


SYSTEM_PROMPT = load_system_prompt()
if SYSTEM_PROMPT:
    print(f"✅ 人设已加载，长度：{len(SYSTEM_PROMPT)} 字符")
else:
    print("ℹ️  无人设，纯转发模式")


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时断开连接"""
    if MEMORY_ENABLED:
        try:
            await init_tables()
            count = await get_all_memories_count()
            print(f"✅ 记忆系统已启动，当前记忆数量：{count}")
        except Exception as e:
            print(f"⚠️  数据库初始化失败: {e}")
            print("⚠️  记忆系统将不可用，但网关仍可正常转发")
    else:
        print("ℹ️  记忆系统已关闭（设置 MEMORY_ENABLED=true 开启）")
    
    yield
    
    if MEMORY_ENABLED:
        await close_pool()


app = FastAPI(title="AI Memory Gateway", version="2.0.0", lifespan=lifespan)

raw = os.getenv("CORS_ORIGINS", "")
origins = [x.strip() for x in raw.split(",") if x.strip()]

app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
)


# ============================================================
# 记忆注入
# ============================================================

def row_get(row: Any, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)


def format_local_datetime(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_dt = value.astimezone(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)
    return local_dt.strftime("%Y-%m-%d %H:%M")


def format_relative_time(value: datetime | None) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value.astimezone(timezone.utc)
    seconds = int(max(delta.total_seconds(), 0))
    if seconds < 3600:
        return f"{max(seconds // 60, 1)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def normalize_messages_for_memory(messages: list[dict]) -> list[dict]:
    normalized = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        content = extract_text_from_content(msg.get("content"))
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def normalize_text_key(text: str) -> str:
    return "".join(text.lower().split())


def is_meta_memory(content: str) -> bool:
    return any(keyword in content for keyword in META_BLACKLIST)


def build_valid_until(valid_until_days: Any) -> datetime | None:
    try:
        days = int(valid_until_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def format_memory_line(memory: Any, include_date: bool = False) -> str:
    content = str(row_get(memory, "content", "") or "").strip()
    if not content:
        return ""
    if not include_date:
        return f"- {content}"
    created_at = row_get(memory, "created_at")
    if not created_at:
        return f"- {content}"
    date_str = format_local_datetime(created_at)
    return f"- [{date_str}] {content}"


def build_memory_lookup(memories: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_content: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for mem in memories:
        content = str(row_get(mem, "content", "") or "").strip()
        canonical_key = str(row_get(mem, "canonical_key", "") or "").strip()
        if content:
            by_content.setdefault(normalize_text_key(content), mem)
        if canonical_key:
            by_key.setdefault(canonical_key, mem)
    return by_content, by_key


async def maybe_promote_memory(memory_id: int):
    memory = await get_memory(memory_id)
    if not memory:
        return
    if row_get(memory, "status") != ACTIVE_STATUS or row_get(memory, "manual_locked", False):
        return

    confirmations = await count_distinct_confirmations(memory_id)
    tier = row_get(memory, "tier")

    if tier == MEMORY_TIER_EPHEMERAL and confirmations >= EPHEMERAL_CONFIRMATIONS_TO_STABLE:
        await update_memory(memory_id, tier=MEMORY_TIER_STABLE)
        print(f"⬆️  记忆 #{memory_id} 已从 ephemeral 升级为 stable")
        memory = await get_memory(memory_id)
        tier = row_get(memory, "tier")

    if tier != MEMORY_TIER_STABLE or row_get(memory, "pending_review", False):
        return

    if confirmations < STABLE_CONFIRMATIONS_TO_REVIEW:
        return

    first_confirmation = await get_first_confirmation_time(memory_id)
    if not first_confirmation:
        return
    if datetime.now(timezone.utc) - first_confirmation < timedelta(days=STABLE_REVIEW_DAYS):
        return

    await update_memory(memory_id, pending_review=True)
    print(f"🪄  stable 记忆 #{memory_id} 已进入 evergreen review queue")


async def save_action_memory(action: dict, source_session: str) -> int | None:
    return await save_memory(
        content=action["content"],
        importance=action.get("importance", 5),
        source_session=source_session,
        tier=action.get("tier", MEMORY_TIER_EPHEMERAL),
        status=ACTIVE_STATUS,
        canonical_key=action.get("canonical_key"),
        valid_until=build_valid_until(action.get("valid_until_days")),
    )


async def handle_memory_conflict(action: dict, source_session: str):
    target_id = action.get("memory_id")
    target = await get_memory(target_id)
    if not target:
        await save_action_memory(action, source_session)
        return

    if row_get(target, "manual_locked", False):
        await save_action_memory(action, source_session)
        print(f"🔒  记忆 #{target_id} 已锁定，冲突内容作为新记忆追加")
        return

    new_memory_id = await save_action_memory(action, source_session)
    if not new_memory_id:
        return

    target_tier = row_get(target, "tier")
    if target_tier == MEMORY_TIER_EPHEMERAL:
        await update_memory(target_id, status="superseded", replaced_by_id=new_memory_id)
        print(f"♻️  临时记忆 #{target_id} 被新记忆 #{new_memory_id} 取代")
    else:
        await update_memory(target_id, status="conflicted")
        print(f"⚠️  记忆 #{target_id} 被标记为 conflicted，新版本为 #{new_memory_id}")


async def summarize_stale_sessions(limit: int = 3):
    stale_sessions = await get_stale_unsummarized_sessions(
        idle_minutes=SESSION_IDLE_MINUTES,
        limit=limit,
    )
    for row in stale_sessions:
        session_id = row_get(row, "session_id")
        msg_count = int(row_get(row, "msg_count", 0) or 0)
        if not session_id:
            continue
        if msg_count < MIN_MESSAGES_FOR_SUMMARY:
            await upsert_session_summary(
                session_id,
                f"{SKIPPED_SUMMARY_PREFIX} {msg_count} 条消息",
                mood=None,
                topic_tags=["short-session"],
                msg_count=msg_count,
            )
            continue

        messages = await get_session_messages(session_id)
        normalized_messages = [
            {"role": row_get(message, "role", ""), "content": row_get(message, "content", "")}
            for message in messages
            if row_get(message, "content", "")
        ]
        summary_data = await summarize_session(normalized_messages)
        summary_text = summary_data.get("summary", "").strip()
        if not summary_text:
            continue
        await upsert_session_summary(
            session_id,
            summary_text,
            mood=summary_data.get("mood"),
            topic_tags=summary_data.get("topic_tags") or [],
            msg_count=msg_count,
        )
        print(f"🧾 已生成 session 摘要: {session_id}")


async def build_system_prompt_with_memories(user_message: str) -> str:
    """
    构建带分层记忆的 system prompt。
    """
    if not MEMORY_ENABLED:
        return SYSTEM_PROMPT

    try:
        evergreen = await get_memories_by_tier(MEMORY_TIER_EVERGREEN, limit=MAX_EVERGREEN_INJECT)
        stable = await search_memories(
            user_message,
            limit=MAX_STABLE_INJECT,
            tiers=[MEMORY_TIER_STABLE],
            statuses=[ACTIVE_STATUS],
        )
        recent_summaries = await get_recent_session_summaries(limit=MAX_SUMMARIES_INJECT)
        open_loops = await get_open_loops(status="open", limit=MAX_OPEN_LOOPS_INJECT)
        ephemeral = await get_memories_by_tier(
            MEMORY_TIER_EPHEMERAL,
            limit=MAX_EPHEMERAL_INJECT,
            days=3,
            touch=False,
        )
        latest_summary_time = await get_latest_summary_time()

        sections = []

        evergreen_lines = [format_memory_line(mem) for mem in evergreen]
        evergreen_lines = [line for line in evergreen_lines if line]
        if evergreen_lines:
            sections.append("【核心长期记忆】\n" + "\n".join(evergreen_lines))

        stable_lines = [format_memory_line(mem, include_date=True) for mem in stable]
        stable_lines = [line for line in stable_lines if line]
        if stable_lines:
            sections.append("【相关稳定记忆】\n" + "\n".join(stable_lines))

        summary_lines = []
        for summary in recent_summaries:
            summary_text = str(row_get(summary, "summary", "") or "").strip()
            if not summary_text or summary_text.startswith(SKIPPED_SUMMARY_PREFIX):
                continue
            mood = str(row_get(summary, "mood", "") or "").strip()
            prefix = f"- ({mood}) " if mood else "- "
            summary_lines.append(prefix + summary_text)
        if summary_lines:
            sections.append("【最近会话摘要】\n" + "\n".join(summary_lines))

        loop_lines = []
        for loop in open_loops:
            content = str(row_get(loop, "content", "") or "").strip()
            if not content:
                continue
            loop_type = str(row_get(loop, "loop_type", "") or "").strip()
            if loop_type:
                loop_lines.append(f"- [{loop_type}] {content}")
            else:
                loop_lines.append(f"- {content}")
        if loop_lines:
            sections.append("【未完事项】\n" + "\n".join(loop_lines))

        ephemeral_lines = [format_memory_line(mem, include_date=True) for mem in ephemeral]
        ephemeral_lines = [line for line in ephemeral_lines if line]
        if ephemeral_lines:
            sections.append("【近期短期状态】\n" + "\n".join(ephemeral_lines))

        if not sections:
            return SYSTEM_PROMPT

        time_lines = [f"- 当前本地时间：{local_now().strftime('%Y-%m-%d %H:%M')}"]
        if latest_summary_time:
            time_lines.append(f"- 最近一段已总结的会话大约在 {format_relative_time(latest_summary_time)} 前。")
        sections.insert(0, "【时间参考】\n" + "\n".join(time_lines))

        if len(sections) == 1 and not SYSTEM_PROMPT:
            return sections[0]

        enhanced_prompt = f"""{SYSTEM_PROMPT}

{chr(10).join(sections)}

# 使用方式
- 这些内容只是辅助你接住上下文，不要机械复述。
- 优先使用核心长期记忆和相关稳定记忆；短期状态只在相关时轻描淡写带一下。
- open loops 是待追问或待完成事项，合适时自然接上。
- 若当前用户消息与旧记忆冲突，以当前明确新信息为准。"""

        total_count = len(evergreen_lines) + len(stable_lines) + len(summary_lines) + len(loop_lines) + len(ephemeral_lines)
        print(f"📚 注入了分层上下文，共 {total_count} 条片段")
        return enhanced_prompt

    except Exception as exc:
        print(f"⚠️  记忆检索失败: {exc}，使用纯人设")
        return SYSTEM_PROMPT


# ============================================================
# 后台记忆处理
# ============================================================

async def process_memories_background(
    session_id: str,
    user_msg: str,
    assistant_msg: str,
    model: str,
    context_messages: list | None = None,
    has_stable_session_id: bool = False,
):
    """
    后台异步：存储对话 + 提取记忆（不阻塞主流程）
    """
    global _round_counter

    try:
        await save_message(session_id, "user", user_msg, model)
        await save_message(session_id, "assistant", assistant_msg, model)

        if MEMORY_EXTRACT_INTERVAL == 0:
            if has_stable_session_id:
                await summarize_stale_sessions()
            print("⏭️  记忆自动提取已禁用，跳过")
            return

        _round_counter += 1
        if MEMORY_EXTRACT_INTERVAL > 1 and (_round_counter % MEMORY_EXTRACT_INTERVAL != 0):
            if has_stable_session_id:
                await summarize_stale_sessions()
            print(f"⏭️  轮次 {_round_counter}，跳过记忆提取（每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次）")
            return

        if MEMORY_EXTRACT_INTERVAL > 1:
            print(f"📝 轮次 {_round_counter}，执行记忆提取")

        if context_messages:
            tail_count = MEMORY_EXTRACT_INTERVAL * 2
            recent_msgs = list(context_messages)[-tail_count:] if len(context_messages) > tail_count else list(context_messages)
            messages_for_extraction = recent_msgs + [{"role": "assistant", "content": assistant_msg}]
            print(f"📝 截取最近 {MEMORY_EXTRACT_INTERVAL} 轮对话提取记忆（{len(messages_for_extraction)} 条消息）")
        else:
            messages_for_extraction = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]

        existing_memories = [dict(row) for row in await get_active_memory_briefs(limit=60)]
        existing_by_content, existing_by_key = build_memory_lookup(existing_memories)
        open_loops = [dict(row) for row in await get_open_loops(status="open", limit=20)]
        extraction_result = await extract_memory_actions(
            messages_for_extraction,
            existing_memories=existing_memories,
            open_loops=open_loops,
        )

        saved_count = 0
        confirmation_count = 0
        conflict_count = 0

        for action in extraction_result["memory_actions"]:
            action_type = action.get("action")
            if action_type in {"create", "conflict"} and is_meta_memory(action["content"]):
                print(f"🚫 过滤掉 meta 记忆: {action['content'][:60]}...")
                continue

            if action_type == "create":
                canonical_key = str(action.get("canonical_key") or "").strip()
                matched = None
                if canonical_key:
                    matched = existing_by_key.get(canonical_key)
                if not matched:
                    matched = existing_by_content.get(normalize_text_key(action["content"]))

                if matched:
                    if has_stable_session_id:
                        matched_id = row_get(matched, "id")
                        if matched_id and await add_memory_confirmation(matched_id, session_id):
                            confirmation_count += 1
                            await maybe_promote_memory(matched_id)
                    else:
                        print("ℹ️  命中已有记忆，但当前请求没有稳定 session_id，跳过 confirm 计数")
                    continue

                new_memory_id = await save_action_memory(action, session_id)
                if new_memory_id:
                    saved_count += 1
                    fresh_memory = {
                        "id": new_memory_id,
                        "content": action["content"],
                        "canonical_key": action.get("canonical_key"),
                    }
                    existing_by_content[normalize_text_key(action["content"])] = fresh_memory
                    if action.get("canonical_key"):
                        existing_by_key[action["canonical_key"]] = fresh_memory
            elif action_type == "confirm":
                if not has_stable_session_id:
                    continue
                memory_id = action.get("memory_id")
                if memory_id and await add_memory_confirmation(memory_id, session_id):
                    confirmation_count += 1
                    await maybe_promote_memory(memory_id)
            elif action_type == "conflict":
                await handle_memory_conflict(action, session_id)
                conflict_count += 1

        loop_creates = extraction_result["open_loops"]["create"]
        for loop in loop_creates:
            await create_open_loop(
                content=loop["content"],
                loop_type=loop.get("loop_type", "promise"),
                source_session=session_id,
            )

        if extraction_result["open_loops"]["resolve"]:
            await resolve_open_loops(extraction_result["open_loops"]["resolve"])

        await expire_old_memories()
        await expire_old_open_loops()
        if has_stable_session_id:
            await summarize_stale_sessions()

        if saved_count or confirmation_count or conflict_count or loop_creates or extraction_result["open_loops"]["resolve"]:
            total = await get_all_memories_count()
            print(
                f"💾 记忆处理完成：新增 {saved_count}，确认 {confirmation_count}，冲突 {conflict_count}，"
                f"open_loops +{len(loop_creates)} / resolved {len(extraction_result['open_loops']['resolve'])}，总计 {total} 条"
            )

    except Exception as exc:
        print(f"⚠️  后台记忆处理失败: {exc}")


# ============================================================
# API 接口
# ============================================================

@app.get("/")
async def health_check():
    """健康检查"""
    memory_count = 0
    if MEMORY_ENABLED:
        try:
            memory_count = await get_all_memories_count()
        except:
            pass
    
    return {
        "status": "running",
        "gateway": "AI Memory Gateway v2.0",
        "system_prompt_loaded": len(SYSTEM_PROMPT) > 0,
        "system_prompt_length": len(SYSTEM_PROMPT),
        "memory_enabled": MEMORY_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": MEMORY_EXTRACT_INTERVAL,
    }


@app.get("/v1/models")
async def list_models():
    """模型列表（让客户端不报错）"""
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ai-memory-gateway",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    if not API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请在环境变量中配置"},
        )
    
    body = await request.json()
    messages = body.get("messages", [])

    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_message = extract_text_from_content(msg.get("content"))
            break

    original_messages = normalize_messages_for_memory(messages)

    if SYSTEM_PROMPT or (MEMORY_ENABLED and user_message):
        if MEMORY_ENABLED and user_message:
            enhanced_prompt = await build_system_prompt_with_memories(user_message)
        else:
            enhanced_prompt = SYSTEM_PROMPT
        
        if enhanced_prompt:
            has_system = any(msg.get("role") == "system" for msg in messages)
            if has_system:
                for i, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        messages[i]["content"] = enhanced_prompt + "\n\n" + msg["content"]
                        break
            else:
                messages.insert(0, {"role": "system", "content": enhanced_prompt})
    
    body["messages"] = messages
    
    # ---------- 模型处理 ----------
    model = body.get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    body["model"] = model

    provided_session_id = str(body.pop("session_id", "") or "").strip()
    has_stable_session_id = bool(provided_session_id)
    session_id = provided_session_id or str(uuid.uuid4())[:8]
    
    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE
    
    is_stream = body.get("stream", False)
    
    if is_stream:
        return StreamingResponse(
            stream_and_capture(
                headers,
                body,
                session_id,
                user_message,
                model,
                original_messages,
                has_stable_session_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(API_BASE_URL, headers=headers, json=body)
            
            if response.status_code == 200:
                resp_data = response.json()
                assistant_msg = ""
                try:
                    assistant_msg = resp_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    pass
                
                if MEMORY_ENABLED and user_message and assistant_msg:
                    asyncio.create_task(
                        process_memories_background(
                            session_id,
                            user_message,
                            assistant_msg,
                            model,
                            context_messages=original_messages,
                            has_stable_session_id=has_stable_session_id,
                        )
                    )
                
                return JSONResponse(status_code=200, content=resp_data)
            else:
                return JSONResponse(status_code=response.status_code, content=response.json())


async def stream_and_capture(
    headers: dict,
    body: dict,
    session_id: str,
    user_message: str,
    model: str,
    original_messages: list | None = None,
    has_stable_session_id: bool = False,
):
    """流式响应 + 捕获完整回复"""
    full_response = []
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", API_BASE_URL, headers=headers, json=body) as response:
            async for line in response.aiter_lines():
                # 透传所有行（包括空行），保持SSE格式完整
                yield line + "\n"
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        data = json.loads(line[6:])
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_response.append(content)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
    
    assistant_msg = "".join(full_response)
    if MEMORY_ENABLED and user_message and assistant_msg:
        asyncio.create_task(
            process_memories_background(
                session_id,
                user_message,
                assistant_msg,
                model,
                context_messages=original_messages,
                has_stable_session_id=has_stable_session_id,
            )
        )


# ============================================================
# 记忆管理接口
# ============================================================


@app.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址就会返回所有记忆数据
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        memories = await get_all_memories()
        for mem in memories:
            for field in ("created_at", "last_accessed", "valid_until"):
                if mem.get(field):
                    mem[field] = str(mem[field])
        
        return {
            "total": len(memories),
            "exported_at": str(__import__("datetime").datetime.now()),
            "memories": memories,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/import/memories", response_class=HTMLResponse)
async def import_memories_page():
    """导入记忆的网页界面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return HTMLResponse("""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>导入记忆</title>
<style>
    body { font-family: sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; }
    textarea { width: 100%%; height: 200px; font-size: 14px; margin: 10px 0; }
    button { padding: 10px 20px; font-size: 16px; cursor: pointer; background: #4CAF50; color: white; border: none; border-radius: 4px; margin-right: 8px; }
    button:hover { background: #45a049; }
    input[type="file"] { margin: 10px 0; font-size: 14px; }
    #result { margin-top: 15px; padding: 10px; white-space: pre-wrap; }
    .ok { background: #e8f5e9; } .err { background: #ffebee; } .info { background: #e3f2fd; }
    .tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid #eee; }
    .tab { padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; color: #666; }
    .tab.active { border-bottom-color: #4CAF50; color: #333; font-weight: bold; }
    .panel { display: none; } .panel.active { display: block; }
    .hint { color: #888; font-size: 13px; margin: 5px 0; }
    label { cursor: pointer; }
    .preview { background: #f5f5f5; border: 1px solid #ddd; padding: 10px; margin: 10px 0; max-height: 200px; overflow-y: auto; font-size: 13px; }
    .preview-item { padding: 3px 0; border-bottom: 1px solid #eee; }
    .nav { margin-bottom: 15px; font-size: 14px; color: #666; }
    .nav a { color: #4CAF50; text-decoration: none; }
</style></head><body>
<h2>📥 导入记忆</h2>
<div class="nav"><a href="/manage/memories">→ 管理已有记忆</a></div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('text')">纯文本导入</div>
    <div class="tab" onclick="switchTab('json')">JSON 备份恢复</div>
</div>

<div id="panel-text" class="panel active">
    <p>上传 <b>.txt 文件</b>（每行一条记忆），或直接在下方输入。</p>
    <p class="hint">示例：一行写一条，比如 "用户的名字叫小花"、"用户喜欢吃火锅"</p>
    <input type="file" id="txtFile" accept=".txt">
    <div style="margin: 15px 0; text-align: center; color: #999;">—— 或者直接输入 ——</div>
    <textarea id="txtInput" placeholder="每行一条记忆，例如：&#10;用户的名字叫小花&#10;用户喜欢吃火锅&#10;用户养了一只狗叫豆豆"></textarea>
    <p><label><input type="checkbox" id="skipScore"> 跳过自动评分（所有记忆默认权重 5，不消耗 API 额度）</label></p>
    <button onclick="doTextImport()">导入</button>
</div>

<div id="panel-json" class="panel">
    <p>上传从 <code>/export/memories</code> 保存的 <b>.json 文件</b>，用于备份恢复或平台迁移。</p>
    <input type="file" id="jsonFile" accept=".json">
    <div style="margin: 15px 0; text-align: center; color: #999;">—— 或者直接粘贴 ——</div>
    <textarea id="jsonInput" placeholder="粘贴导出的 JSON"></textarea>
    <br><button onclick="previewJson()">预览</button>
    <div id="jsonPreview"></div>
</div>

<div id="result"></div>

<script>
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    document.getElementById('result').textContent = '';
    document.getElementById('result').className = '';
    document.getElementById('jsonPreview').innerHTML = '';
}

async function doTextImport() {
    const r = document.getElementById('result');
    const file = document.getElementById('txtFile').files[0];
    const text = document.getElementById('txtInput').value.trim();
    const skip = document.getElementById('skipScore').checked;
    
    let content = '';
    if (file) { content = await file.text(); }
    else if (text) { content = text; }
    else { r.className = 'err'; r.textContent = '请先上传文件或输入文本'; return; }
    
    const lines = content.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
    if (lines.length === 0) { r.className = 'err'; r.textContent = '没有找到有效的记忆条目'; return; }
    
    r.className = 'info';
    r.textContent = skip ? '正在导入 ' + lines.length + ' 条记忆...' : '正在为 ' + lines.length + ' 条记忆自动评分，请稍候...';
    
    try {
        const resp = await fetch('/import/text', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lines: lines, skip_scoring: skip})
        });
        const data = await resp.json();
        if (data.error) { r.className = 'err'; r.textContent = '❌ ' + data.error; }
        else { r.className = 'ok'; r.textContent = '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条'; }
    } catch(e) { r.className = 'err'; r.textContent = '❌ 请求失败：' + e.message; }
}

let pendingJsonData = null;

async function previewJson() {
    const r = document.getElementById('result');
    const p = document.getElementById('jsonPreview');
    const file = document.getElementById('jsonFile').files[0];
    const text = document.getElementById('jsonInput').value.trim();
    
    let jsonStr = '';
    if (file) { jsonStr = await file.text(); }
    else if (text) { jsonStr = text; }
    else { r.className = 'err'; r.textContent = '请先上传文件或粘贴 JSON'; return; }
    
    try {
        const parsed = JSON.parse(jsonStr);
        const mems = parsed.memories || [];
        if (mems.length === 0) { r.className = 'err'; r.textContent = '❌ 没有找到 memories 字段，请确认这是从 /export/memories 导出的文件'; p.innerHTML = ''; return; }
        
        pendingJsonData = parsed;
        let html = '<p><b>预览：共 ' + mems.length + ' 条记忆</b></p>';
        const show = mems.slice(0, 10);
        show.forEach(m => { html += '<div class="preview-item">权重 ' + (m.importance || '?') + ' | ' + (m.content || '').substring(0, 80) + '</div>'; });
        if (mems.length > 10) html += '<div class="preview-item" style="color:#999;">...还有 ' + (mems.length - 10) + ' 条</div>';
        html += '<br><button onclick="confirmJsonImport()">确认导入</button>';
        p.innerHTML = html;
        r.textContent = ''; r.className = '';
    } catch(e) { r.className = 'err'; r.textContent = '❌ JSON 格式错误：' + e.message; p.innerHTML = ''; }
}

async function confirmJsonImport() {
    const r = document.getElementById('result');
    if (!pendingJsonData) { r.className = 'err'; r.textContent = '请先预览'; return; }
    
    r.className = 'info'; r.textContent = '导入中...';
    try {
        const resp = await fetch('/import/memories', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(pendingJsonData)
        });
        const data = await resp.json();
        if (data.error) { r.className = 'err'; r.textContent = '❌ ' + data.error; }
        else { r.className = 'ok'; r.textContent = '✅ 导入完成！新增 ' + data.imported + ' 条，跳过 ' + data.skipped + ' 条（已存在），总计 ' + data.total + ' 条'; }
        document.getElementById('jsonPreview').innerHTML = '';
        pendingJsonData = null;
    } catch(e) { r.className = 'err'; r.textContent = '❌ 请求失败：' + e.message; }
}
</script></body></html>
""")


@app.get("/manage/memories", response_class=HTMLResponse)
async def manage_memories_page():
    """记忆管理页面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return HTMLResponse("""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>管理记忆</title>
<style>
    body { font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }
    .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 15px; flex-wrap: wrap; }
    input[type="text"] { padding: 8px 12px; font-size: 14px; border: 1px solid #ddd; border-radius: 4px; width: 250px; }
    button { padding: 8px 16px; font-size: 14px; cursor: pointer; border: none; border-radius: 4px; }
    .btn-green { background: #4CAF50; color: white; } .btn-green:hover { background: #45a049; }
    .btn-red { background: #f44336; color: white; } .btn-red:hover { background: #d32f2f; }
    .btn-gray { background: #9e9e9e; color: white; } .btn-gray:hover { background: #757575; }
    table { width: 100%%; border-collapse: collapse; font-size: 14px; }
    th { background: #f5f5f5; padding: 10px 8px; text-align: left; border-bottom: 2px solid #ddd; position: sticky; top: 0; }
    td { padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; }
    tr:hover { background: #fafafa; }
    .content-cell { max-width: 450px; word-break: break-all; }
    .importance-input { width: 45px; padding: 4px; text-align: center; border: 1px solid #ddd; border-radius: 3px; }
    .content-input { width: 100%%; padding: 4px; border: 1px solid #ddd; border-radius: 3px; font-size: 13px; min-height: 40px; resize: vertical; }
    .actions button { padding: 4px 8px; font-size: 12px; margin: 2px; }
    .msg { padding: 10px; margin-bottom: 10px; border-radius: 4px; }
    .ok { background: #e8f5e9; } .err { background: #ffebee; } .info { background: #e3f2fd; }
    .stats { color: #666; font-size: 14px; margin-bottom: 10px; }
    .nav { margin-bottom: 15px; font-size: 14px; color: #666; }
    .nav a { color: #4CAF50; text-decoration: none; }
    .check-col { width: 30px; text-align: center; }
    .id-col { width: 40px; }
    .imp-col { width: 60px; }
    .source-col { width: 90px; font-size: 12px; color: #888; }
    .time-col { width: 140px; font-size: 12px; color: #888; white-space: nowrap; }
    .actions-col { width: 120px; }
</style></head><body>
<h2>🧠 记忆管理</h2>
<div class="nav"><a href="/import/memories">→ 导入新记忆</a> ｜ <a href="/export/memories">→ 导出备份</a></div>

<div class="toolbar">
    <input type="text" id="searchBox" placeholder="搜索记忆..." oninput="filterAndSort()">
    <input type="date" id="dateFilter" onchange="filterAndSort()" style="padding:7px 10px;font-size:14px;border:1px solid #ddd;border-radius:4px;" title="按日期筛选">
    <button class="btn-gray" onclick="document.getElementById('dateFilter').value='';filterAndSort()" style="padding:7px 10px;font-size:12px;" title="清除日期">✕</button>
    <select id="sortSelect" onchange="filterAndSort()" style="padding:8px 12px;font-size:14px;border:1px solid #ddd;border-radius:4px;">
        <option value="id-desc">ID 从新到旧</option>
        <option value="id-asc">ID 从旧到新</option>
        <option value="imp-desc">权重 从高到低</option>
        <option value="imp-asc">权重 从低到高</option>
    </select>
    <button class="btn-green" onclick="batchSave()">批量保存全部</button>
    <button class="btn-red" onclick="batchDelete()">批量删除选中</button>
    <label style="font-size:13px;color:#666;cursor:pointer;"><input type="checkbox" id="selectAll" onchange="toggleAll()"> 全选</label>
</div>
<div id="msg"></div>
<div class="stats" id="stats"></div>
<div style="overflow-x: auto;">
<table>
    <thead><tr>
        <th class="check-col"><input type="checkbox" id="selectAllHead" onchange="toggleAll()"></th>
        <th class="id-col">ID</th>
        <th>内容</th>
        <th class="imp-col">权重</th>
        <th class="source-col">来源</th>
        <th class="time-col">时间</th>
        <th class="actions-col">操作</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
</table>
</div>

<script>
let allMemories = [];

async function loadMemories() {
    try {
        const resp = await fetch('/api/memories');
        const data = await resp.json();
        allMemories = data.memories || [];
        document.getElementById('stats').textContent = '共 ' + allMemories.length + ' 条记忆';
        filterAndSort();
    } catch(e) { showMsg('err', '加载失败：' + e.message); }
}

function fmtTime(s) {
    if (!s) return '-';
    var d = new Date(s.endsWith('Z') ? s : s + 'Z');
    if (isNaN(d)) return s.slice(0, 19).replace('T', ' ');
    var pad = function(n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function renderTable(mems) {
    const tbody = document.getElementById('tbody');
    tbody.innerHTML = mems.map(m => '<tr data-id="' + m.id + '">' +
        '<td class="check-col"><input type="checkbox" class="mem-check" value="' + m.id + '"></td>' +
        '<td class="id-col">' + m.id + '</td>' +
        '<td class="content-cell"><textarea class="content-input" id="c_' + m.id + '">' + escHtml(m.content) + '</textarea></td>' +
        '<td><input type="number" class="importance-input" id="i_' + m.id + '" value="' + m.importance + '" min="1" max="10"></td>' +
        '<td class="source-col">' + (m.source_session || '-') + '</td>' +
        '<td class="time-col">' + fmtTime(m.created_at) + '</td>' +
        '<td class="actions"><button class="btn-green" onclick="saveMem(' + m.id + ')">保存</button><button class="btn-red" onclick="delMem(' + m.id + ')">删除</button></td>' +
        '</tr>').join('');
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function filterAndSort() {
    const q = document.getElementById('searchBox').value.trim().toLowerCase();
    const sort = document.getElementById('sortSelect').value;
    const dateVal = document.getElementById('dateFilter').value;
    let mems = allMemories;
    if (q) {
        mems = mems.filter(m => m.content.toLowerCase().includes(q));
    }
    if (dateVal) {
        mems = mems.filter(m => m.created_at && fmtTime(m.created_at).slice(0, 10) === dateVal);
    }
    mems = [...mems].sort((a, b) => {
        if (sort === 'id-desc') return b.id - a.id;
        if (sort === 'id-asc') return a.id - b.id;
        if (sort === 'imp-desc') return b.importance - a.importance || b.id - a.id;
        if (sort === 'imp-asc') return a.importance - b.importance || a.id - b.id;
        return 0;
    });
    renderTable(mems);
    const parts = [];
    if (q || dateVal) {
        parts.push('筛选到 ' + mems.length + ' / ' + allMemories.length + ' 条');
        if (dateVal) parts.push('日期: ' + dateVal);
    } else {
        parts.push('共 ' + allMemories.length + ' 条记忆');
    }
    document.getElementById('stats').textContent = parts.join('  ');
}

async function saveMem(id) {
    const content = document.getElementById('c_' + id).value;
    const importance = parseInt(document.getElementById('i_' + id).value);
    try {
        const resp = await fetch('/api/memories/' + id, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content, importance})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已保存 #' + id); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function delMem(id) {
    if (!confirm('确定删除 #' + id + '？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/memories/' + id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已删除 #' + id); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function batchSave() {
    const rows = document.querySelectorAll('#tbody tr');
    if (rows.length === 0) { showMsg('err', '没有记忆可保存'); return; }
    const updates = [];
    rows.forEach(row => {
        const id = parseInt(row.dataset.id);
        const cEl = document.getElementById('c_' + id);
        const iEl = document.getElementById('i_' + id);
        if (cEl && iEl) updates.push({id, content: cEl.value, importance: parseInt(iEl.value)});
    });
    if (!confirm('确定保存全部 ' + updates.length + ' 条记忆的修改？')) return;
    try {
        const resp = await fetch('/api/memories/batch-update', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({updates: updates})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已保存 ' + data.updated + ' 条'); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

async function batchDelete() {
    const checked = [...document.querySelectorAll('.mem-check:checked')].map(c => parseInt(c.value));
    if (checked.length === 0) { showMsg('err', '请先勾选要删除的记忆'); return; }
    if (!confirm('确定删除选中的 ' + checked.length + ' 条记忆？此操作不可撤销。')) return;
    try {
        const resp = await fetch('/api/memories/batch-delete', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids: checked})
        });
        const data = await resp.json();
        if (data.error) showMsg('err', '❌ ' + data.error);
        else { showMsg('ok', '✅ 已删除 ' + data.deleted + ' 条'); loadMemories(); }
    } catch(e) { showMsg('err', '❌ ' + e.message); }
}

function toggleAll() {
    const val = event.target.checked;
    document.querySelectorAll('.mem-check').forEach(c => c.checked = val);
    document.getElementById('selectAll').checked = val;
    document.getElementById('selectAllHead').checked = val;
}

function showMsg(cls, text) {
    const el = document.getElementById('msg');
    el.className = 'msg ' + cls;
    el.textContent = text;
    setTimeout(() => { el.textContent = ''; el.className = ''; }, 4000);
}

loadMemories();
</script></body></html>
""")


# ============================================================
# 管理 API
# ============================================================

@app.get("/api/memories")
async def api_get_memories():
    """获取所有记忆（管理页面用）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    memories = await get_all_memories_detail()
    for m in memories:
        for field in ("created_at", "last_accessed", "valid_until"):
            if m.get(field):
                m[field] = str(m[field])
    return {"memories": memories}


@app.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await update_memory(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
    )
    return {"status": "ok", "id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int):
    """删除单条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    await delete_memory(memory_id)
    return {"status": "ok", "id": memory_id}


@app.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await update_memory(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
        )
    return {"status": "ok", "updated": len(updates)}


@app.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量删除记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    await delete_memories_batch(ids)
    return {"status": "ok", "deleted": len(ids)}


@app.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)
        
        if not lines:
            return {"error": "没有找到记忆条目"}
        
        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            scored = await score_memories(lines)
        
        imported = 0
        skipped = 0
        
        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        memories = data.get("memories", [])
        
        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}
        
        imported = 0
        skipped = 0
        
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
                tier=mem.get("tier", MEMORY_TIER_EPHEMERAL),
                status=mem.get("status", ACTIVE_STATUS),
                canonical_key=mem.get("canonical_key"),
                manual_locked=bool(mem.get("manual_locked", False)),
                pending_review=bool(mem.get("pending_review", False)),
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 AI Memory Gateway 启动中... 端口 {PORT}")
    print(f"📝 人设长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"🤖 默认模型：{DEFAULT_MODEL}")
    print(f"🔗 API 地址：{API_BASE_URL}")
    print(f"🧠 记忆系统：{'开启' if MEMORY_ENABLED else '关闭'}")
    print(f"🔄 记忆提取间隔：{'禁用' if MEMORY_EXTRACT_INTERVAL == 0 else '每轮提取' if MEMORY_EXTRACT_INTERVAL == 1 else f'每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
