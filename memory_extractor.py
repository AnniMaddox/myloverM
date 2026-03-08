"""
记忆提取模块 —— 负责把对话压成可执行的记忆动作
================================================
这里不直接决定数据库怎么写，而是让模型返回统一 JSON：
- memory_actions: create / confirm / conflict
- open_loops: create / resolve
- session summary: 独立函数生成
"""

import json
import os
from typing import Any, Dict, List, Sequence

import httpx

API_KEY = os.getenv("API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")


EXTRACTION_PROMPT = """你是记忆系统的提取器。你的任务不是闲聊，而是把对话转换成结构化动作。

# 已有记忆
<existing_memories>
{existing_memories}
</existing_memories>

# 当前未完事项
<open_loops>
{open_loops}
</open_loops>

# 规则
1. 只根据用户明确表达的新信息做判断；assistant 单方面的猜测、复述、引导，不算 confirm。
2. 如果用户明确重复、确认了已有记忆，返回 confirm。
3. 如果用户给出与已有记忆冲突的新事实，返回 conflict。
4. 如果是新的信息，返回 create。
5. create 的 tier 只允许：
   - stable: 稳定偏好、长期事实、长期边界、长期互动规则
   - ephemeral: 当下状态、短期安排、临时事件、最近发生的小事
   不要输出 evergreen。
6. open_loops 只记录承诺、待追问、还没收尾的话题；普通事实不要塞进去。
7. 忽略这些内容：
   - 日常寒暄
   - 记忆系统、数据库、提取逻辑等元讨论
   - 纯技术调试过程
   - assistant 的思维链或自我评价

# 输出 JSON schema
只返回 JSON 对象，不要 markdown，不要解释：
{
  "memory_actions": [
    {
      "action": "create",
      "content": "用户不吃香菜",
      "importance": 8,
      "tier": "stable",
      "canonical_key": null,
      "valid_until_days": null
    },
    {
      "action": "confirm",
      "memory_id": 12
    },
    {
      "action": "conflict",
      "memory_id": 18,
      "content": "用户现在更喜欢黑咖啡",
      "importance": 7,
      "tier": "stable",
      "canonical_key": null,
      "valid_until_days": null
    }
  ],
  "open_loops": {
    "create": [
      {
        "content": "下次记得问用户考试结果",
        "loop_type": "follow_up"
      }
    ],
    "resolve": [3, 5]
  }
}

如果没有内容，返回：
{
  "memory_actions": [],
  "open_loops": {
    "create": [],
    "resolve": []
  }
}
"""


SUMMARY_PROMPT = """你是会话摘要助手。请把下面这段对话压缩成适合下次续聊的 session 摘要。

# 要求
- 第三人称描述
- 保留：主要话题、情绪氛围、未收尾的话题
- 不要把长期事实写成一大串档案卡
- 语气自然、简洁

# 输出 JSON
{
  "summary": "......",
  "mood": "轻松/紧张/甜蜜/疲惫/平静/混合",
  "topic_tags": ["标签1", "标签2", "标签3"]
}

只返回 JSON。
"""


SCORING_PROMPT = """你是记忆重要性评分专家。请对以下记忆条目逐条评分。

# 评分规则（1-10）
- 9-10：核心身份信息（名字、生日、职业、重要关系）
- 7-8：重要偏好、重大事件、深层情感
- 5-6：日常习惯、一般偏好
- 3-4：临时状态、偶然提及
- 1-2：琐碎信息

# 输入记忆
{memories_text}

# 输出格式
返回 JSON 数组，每条包含原文和评分：
[{{"content": "原文", "importance": 评分数字}}]

只返回 JSON，不要其他文字。"""


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _clamp_importance(value: Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(10, score))


def _normalize_tier(value: Any) -> str:
    if value == "stable":
        return "stable"
    return "ephemeral"


def _normalize_loop_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "promise"
    return value.strip()[:40]


def _normalize_valid_until_days(value: Any) -> int | None:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def _format_messages(messages: Sequence[Dict[str, str]]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"用户: {content}")
        elif role == "assistant":
            lines.append(f"AI: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _format_existing_memories(existing_memories: Sequence[Dict[str, Any]] | None) -> str:
    if not existing_memories:
        return "（暂无已知记忆）"

    lines = []
    for mem in existing_memories:
        mem_id = mem.get("id")
        content = str(mem.get("content") or mem.get("brief") or "").strip()
        if not content:
            continue
        tier = mem.get("tier") or "ephemeral"
        importance = mem.get("importance", 5)
        lines.append(f"- #{mem_id} [{tier}] ({importance}) {content}")
    return "\n".join(lines) if lines else "（暂无已知记忆）"


def _format_open_loops(open_loops: Sequence[Dict[str, Any]] | None) -> str:
    if not open_loops:
        return "（暂无未完事项）"

    lines = []
    for loop in open_loops:
        loop_id = loop.get("id")
        loop_type = loop.get("loop_type") or "promise"
        content = str(loop.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- #{loop_id} [{loop_type}] {content}")
    return "\n".join(lines) if lines else "（暂无未完事项）"


async def _call_memory_model(system_prompt: str, user_prompt: str, max_tokens: int = 1500) -> str:
    if not API_KEY:
        print("⚠️  API_KEY 未设置，跳过记忆提取")
        return ""

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            API_BASE_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://midsummer-gateway.local",
                "X-Title": "Midsummer Memory Extraction",
            },
            json={
                "model": MEMORY_MODEL,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )

    if response.status_code != 200:
        print(f"⚠️  记忆提取请求失败: {response.status_code}")
        return ""

    data = response.json()
    return _strip_code_fences(data.get("choices", [{}])[0].get("message", {}).get("content", ""))


def _sanitize_extraction_result(payload: Any) -> Dict[str, Any]:
    result = {
        "memory_actions": [],
        "open_loops": {
            "create": [],
            "resolve": [],
        },
    }

    if not isinstance(payload, dict):
        return result

    actions = payload.get("memory_actions", [])
    if isinstance(actions, list):
        for raw_action in actions:
            if not isinstance(raw_action, dict):
                continue
            action = str(raw_action.get("action", "")).strip().lower()
            if action == "create":
                content = str(raw_action.get("content", "")).strip()
                if not content:
                    continue
                result["memory_actions"].append(
                    {
                        "action": "create",
                        "content": content,
                        "importance": _clamp_importance(raw_action.get("importance", 5)),
                        "tier": _normalize_tier(raw_action.get("tier")),
                        "canonical_key": raw_action.get("canonical_key") if isinstance(raw_action.get("canonical_key"), str) else None,
                        "valid_until_days": _normalize_valid_until_days(raw_action.get("valid_until_days")),
                    }
                )
            elif action == "confirm":
                try:
                    memory_id = int(raw_action.get("memory_id"))
                except (TypeError, ValueError):
                    continue
                result["memory_actions"].append(
                    {
                        "action": "confirm",
                        "memory_id": memory_id,
                    }
                )
            elif action == "conflict":
                try:
                    memory_id = int(raw_action.get("memory_id"))
                except (TypeError, ValueError):
                    continue
                content = str(raw_action.get("content", "")).strip()
                if not content:
                    continue
                result["memory_actions"].append(
                    {
                        "action": "conflict",
                        "memory_id": memory_id,
                        "content": content,
                        "importance": _clamp_importance(raw_action.get("importance", 5)),
                        "tier": _normalize_tier(raw_action.get("tier")),
                        "canonical_key": raw_action.get("canonical_key") if isinstance(raw_action.get("canonical_key"), str) else None,
                        "valid_until_days": _normalize_valid_until_days(raw_action.get("valid_until_days")),
                    }
                )

    loops = payload.get("open_loops", {})
    if isinstance(loops, dict):
        raw_creates = loops.get("create", [])
        if isinstance(raw_creates, list):
            for loop in raw_creates:
                if not isinstance(loop, dict):
                    continue
                content = str(loop.get("content", "")).strip()
                if not content:
                    continue
                result["open_loops"]["create"].append(
                    {
                        "content": content,
                        "loop_type": _normalize_loop_type(loop.get("loop_type")),
                    }
                )

        raw_resolves = loops.get("resolve", [])
        if isinstance(raw_resolves, list):
            for loop_id in raw_resolves:
                try:
                    normalized = int(loop_id)
                except (TypeError, ValueError):
                    continue
                result["open_loops"]["resolve"].append(normalized)

    return result


async def extract_memory_actions(
    messages: List[Dict[str, str]],
    existing_memories: Sequence[Dict[str, Any]] | None = None,
    open_loops: Sequence[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """
    从对话中提取结构化动作：
    - create / confirm / conflict
    - open loop 的创建与关闭
    """
    if not messages:
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }

    conversation_text = _format_messages(messages)
    if not conversation_text.strip():
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }

    prompt = EXTRACTION_PROMPT.format(
        existing_memories=_format_existing_memories(existing_memories),
        open_loops=_format_open_loops(open_loops),
    )

    try:
        text = await _call_memory_model(
            prompt,
            f"请分析以下对话，并输出统一 JSON：\n\n{conversation_text}",
            max_tokens=1800,
        )
        payload = json.loads(text) if text else {}
        result = _sanitize_extraction_result(payload)
        print(
            "📝 提取动作："
            f"{len(result['memory_actions'])} 条记忆动作，"
            f"{len(result['open_loops']['create'])} 个新增 open loop，"
            f"{len(result['open_loops']['resolve'])} 个 resolved open loop"
        )
        return result
    except json.JSONDecodeError as exc:
        print(f"⚠️  记忆动作 JSON 解析失败: {exc}")
        print(f"🔍 模型原始回傳: {text[:400]}")
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }
    except Exception as exc:
        print(f"⚠️  记忆动作提取失败: {exc}")
        return {
            "memory_actions": [],
            "open_loops": {"create": [], "resolve": []},
        }


async def summarize_session(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """生成 session 级摘要，供下次续聊时注入。"""
    if not messages:
        return {"summary": "", "mood": None, "topic_tags": []}

    conversation_text = _format_messages(messages)
    if not conversation_text.strip():
        return {"summary": "", "mood": None, "topic_tags": []}

    try:
        text = await _call_memory_model(
            SUMMARY_PROMPT,
            f"请为下面这段会话生成摘要：\n\n{conversation_text}",
            max_tokens=900,
        )
        payload = json.loads(text) if text else {}
        if not isinstance(payload, dict):
            return {"summary": "", "mood": None, "topic_tags": []}
        summary = str(payload.get("summary", "")).strip()
        mood = payload.get("mood")
        topic_tags = payload.get("topic_tags", [])
        if not isinstance(topic_tags, list):
            topic_tags = []
        topic_tags = [str(tag).strip() for tag in topic_tags if str(tag).strip()][:6]
        return {
            "summary": summary,
            "mood": str(mood).strip() if isinstance(mood, str) and mood.strip() else None,
            "topic_tags": topic_tags,
        }
    except Exception as exc:
        print(f"⚠️  session 摘要生成失败: {exc}")
        return {"summary": "", "mood": None, "topic_tags": []}


async def extract_memories(messages: List[Dict[str, str]], existing_memories: List[str] | None = None) -> List[Dict[str, Any]]:
    """
    兼容旧接口：只返回 create 动作，避免旧调用直接炸掉。
    """
    existing = [{"id": None, "content": text, "tier": "ephemeral", "importance": 5} for text in (existing_memories or [])]
    result = await extract_memory_actions(messages, existing_memories=existing, open_loops=None)
    memories: list[Dict[str, Any]] = []
    for action in result["memory_actions"]:
        if action["action"] != "create":
            continue
        memories.append(
            {
                "content": action["content"],
                "importance": action["importance"],
            }
        )
    return memories


async def score_memories(texts: List[str]) -> List[Dict[str, Any]]:
    """对纯文本记忆条目批量评分。"""
    if not texts:
        return []

    memories_text = "\n".join(f"- {text}" for text in texts)
    prompt = SCORING_PROMPT.format(memories_text=memories_text)

    try:
        text = await _call_memory_model(prompt, "请返回评分结果。", max_tokens=1200)
        payload = json.loads(text) if text else []
        if not isinstance(payload, list):
            raise ValueError("score payload is not a list")

        scored = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            scored.append(
                {
                    "content": content,
                    "importance": _clamp_importance(item.get("importance", 5)),
                }
            )
        if scored:
            print(f"📝 为 {len(scored)} 条记忆完成自动评分")
            return scored
    except Exception as exc:
        print(f"⚠️  记忆评分出错: {exc}")

    return [{"content": text, "importance": 5} for text in texts]
