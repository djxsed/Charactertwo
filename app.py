import discord
from discord.ext import commands
import os
import aiosqlite
import re
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta
import hashlib
import uuid
import asyncio
from collections import deque
from flask import Flask
import threading

# Flask 웹 서버 설정
app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is running!"

# 환경 변수 불러오기
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI API 설정
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# 봇 설정
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# 상수 정의
BANNED_WORDS = ["악마", "천사", "이세계", "드래곤"]
MIN_LENGTH = 50
REQUIRED_FIELDS = ["이름:", "나이:", "성격:"]
LOG_CHANNEL_ID = 1358060156742533231
COOLDOWN_SECONDS = 5
MAX_REQUESTS_PER_DAY = 1000

# 기본 설정값
DEFAULT_ALLOWED_RACES = ["인간", "마법사", "요괴"]
DEFAULT_ALLOWED_ROLES = ["학생", "선생님", "AML"]
DEFAULT_CHECK_CHANNEL_NAME = "입학-신청서"

# 숫자 속성 체크용 정규 표현식
NUMBER_PATTERN = r"\b(체력|지능|이동속도|힘)\s*:\s*([1-6])\b|\b냉철\s*:\s*([1-4])\b|\[\w+\]\s*\((\d)\)"
AGE_PATTERN = r"나이:\s*(\d+)"

# 기본 프롬프트
DEFAULT_PROMPT = """
디스코드 역할극 서버의 캐릭터 심사 봇이야. 캐릭터 설명을 보고:
1. 서버 규칙에 맞는지 판단해.
2. 캐릭터가 {allowed_roles} 중 하나인지 정해.
**간결하게 50자 이내로 답변해.**

**규칙**:
- 금지 단어: {banned_words} (이미 확인됨).
- 필수 항목: {required_fields} (이미 확인됨).
- 허용 종족: {allowed_races}.
- 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~5) (이미 확인됨).
- 설명은 현실적이고 역할극에 적합해야 해.
- 시간/현실 조작 능력 금지.
- 과거사: 시간 여행, 초자연적 능력, 비현실적 사건(예: 세계 구함) 금지.
- 나이: 1~5000살 (이미 확인됨).
- 소속: A.M.L, 하람고, 하람고등학교만 허용.
- 속성 합산(체력, 지능, 이동속도, 힘, 냉철): 인간 5~16, 마법사 5~17, 요괴 5~18.

**역할 판단**:
1. 소속에 'AML' 포함 → AML.
2. 소속에 '선생'/'선생님' 포함 → 선생님.
3. 소속에 '학생' 포함 → 학생.
4. 모호하면 실패.

**캐릭터 설명**:
{description}

**응답 형식**:
- 통과: "✅ 역할: [역할]"
- 실패: "❌ [실패 이유]"
"""

# 질문 목록
questions = [
    {
        "field": "종족",
        "prompt": "종족을 입력해주세요. (인간, 마법사, 요괴 중 하나)",
        "validator": lambda x: x in ["인간", "마법사", "요괴"],
        "error_message": "허용되지 않은 종족입니다. 인간, 마법사, 요괴 중에서 선택해주세요."
    },
    {
        "field": "이름",
        "prompt": "캐릭터의 이름을 입력해주세요.",
        "validator": lambda x: len(x) > 0,
        "error_message": "이름을 입력해주세요."
    },
    {
        "field": "성별",
        "prompt": "성별을 입력해주세요.",
        "validator": lambda x: True,
        "error_message": ""
    },
    {
        "field": "나이",
        "prompt": "나이를 입력해주세요. (1~5000)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 5000,
        "error_message": "나이는 1에서 5000 사이의 숫자여야 합니다."
    },
    {
        "field": "키/몸무게",
        "prompt": "키와 몸무게를 입력해주세요. (예: 170cm/60kg)",
        "validator": lambda x: True,
        "error_message": ""
    },
    {
        "field": "성격",
        "prompt": "성격을 설명해주세요. (최소 10자)",
        "validator": lambda x: len(x) >= 10,
        "error_message": "성격 설명이 너무 짧습니다. 최소 10자 이상 입력해주세요."
    },
    {
        "field": "체력",
        "prompt": "체력 수치를 입력해주세요. (1~6)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6,
        "error_message": "체력은 1에서 6 사이의 숫자여야 합니다."
    },
    {
        "field": "지능",
        "prompt": "지능 수치를 입력해주세요. (1~6)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6,
        "error_message": "지능은 1에서 6 사이의 숫자여야 합니다."
    },
    {
        "field": "이동속도",
        "prompt": "이동속도 수치를 입력해주세요. (1~6)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6,
        "error_message": "이동속도는 1에서 6 사이의 숫자여야 합니다."
    },
    {
        "field": "힘",
        "prompt": "힘 수치를 입력해주세요. (1~6)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6,
        "error_message": "힘은 1에서 6 사이의 숫자여야 합니다."
    },
    {
        "field": "냉철",
        "prompt": "냉철 수치를 입력해주세요. (1~4)",
        "validator": lambda x: x.isdigit() and 1 <= int(x) <= 4,
        "error_message": "냉철은 1에서 4 사이의 숫자여야 합니다."
    },
    {
        "field": "기술/마법/요력",
        "prompt": "기술/마법/요력을 설명해주세요. (위력 1~5)",
        "validator": lambda x: len(x) > 0,
        "error_message": "기술/마법/요력을 입력해주세요."
    },
    {
        "field": "과거사",
        "prompt": "과거사를 설명해주세요. (최소 20자)",
        "validator": lambda x: len(x) >= 20,
        "error_message": "과거사 설명이 너무 짧습니다. 최소 20자 이상 입력해주세요."
    },
    {
        "field": "소속",
        "prompt": "소속을 입력해주세요. (A.M.L, 하람고, 하람고등학교 중 하나)",
        "validator": lambda x: x in ["A.M.L", "하람고", "하람고등학교"],
        "error_message": "허용되지 않은 소속입니다. A.M.L, 하람고, 하람고등학교 중에서 선택해주세요."
    },
]

# Flex 작업 큐
flex_queue = deque()

# 데이터베이스 초기화
async def init_db():
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                character_id TEXT PRIMARY KEY,
                description_hash TEXT,
                pass BOOLEAN,
                reason TEXT,
                role_name TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id TEXT PRIMARY KEY,
                last_request TEXT,
                request_count INTEGER,
                reset_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flex_tasks (
                task_id TEXT PRIMARY KEY,
                character_id TEXT,
                description TEXT,
                user_id TEXT,
                channel_id TEXT,
                thread_id TEXT,
                type TEXT,
                prompt TEXT,
                status TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                guild_id TEXT PRIMARY KEY,
                prompt_content TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id TEXT PRIMARY KEY,
                allowed_roles TEXT,
                check_channel_name TEXT
            )
        """)
        await db.commit()

# 서버별 설정 조회
async def get_settings(guild_id):
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT allowed_roles, check_channel_name FROM settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                allowed_roles = row[0].split(",") if row[0] else DEFAULT_ALLOWED_ROLES
                check_channel_name = row[1] if row[1] else DEFAULT_CHECK_CHANNEL_NAME
                return allowed_roles, check_channel_name
            return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME

# 서버별 프롬프트 조회
async def get_prompt(guild_id, allowed_roles):
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT prompt_content FROM prompts WHERE guild_id = ?", (str(guild_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            return DEFAULT_PROMPT.format(
                banned_words=', '.join(BANNED_WORDS),
                required_fields=', '.join(REQUIRED_FIELDS),
                allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
                allowed_roles=', '.join(allowed_roles),
                description="{description}"
            )

# 쿨다운 및 요청 횟수 체크
async def check_cooldown(user_id):
    now = datetime.utcnow()
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT last_request, request_count, reset_date FROM cooldowns WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.execute("INSERT INTO cooldowns (user_id, last_request, request_count, reset_date) VALUES (?, ?, ?, ?)",
                                 (user_id, now.isoformat(), 1, now.date().isoformat()))
                await db.commit()
                return True, ""
            
            last_request, request_count, reset_date = row
            last_request = datetime.fromisoformat(last_request)
            reset_date = datetime.fromisoformat(reset_date).date()

            if reset_date < now.date():
                await db.execute("UPDATE cooldowns SET request_count = 0, reset_date = ? WHERE user_id = ?",
                                 (now.date().isoformat(), user_id))
                request_count = 0

            if request_count >= MAX_REQUESTS_PER_DAY:
                return False, f"❌ 하루 최대 {MAX_REQUESTS_PER_DAY}번이야! 내일 다시 와~ 😊"
            
            if (now - last_request).total_seconds() < COOLDOWN_SECONDS:
                return False, f"❌ {COOLDOWN_SECONDS}초 더 기다려야 해~ 😅"

            await db.execute("UPDATE cooldowns SET last_request = ?, request_count = ? WHERE user_id = ?",
                             (now.isoformat(), request_count + 1, user_id))
            await db.commit()
            return True, ""

# 추가 검증 함수
def validate_all(answers):
    errors = []
    race = answers["종족"]
    attributes = [int(answers[attr]) for attr in ["체력", "지능", "이동속도", "힘", "냉철"]]
    attr_sum = sum(attributes)
    if race == "인간" and not (5 <= attr_sum <= 16):
        errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "인간의 속성 합계는 5~16이어야 합니다."))
    elif race == "마법사" and not (5 <= attr_sum <= 17):
        errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "마법사의 속성 합계는 5~17이어야 합니다."))
    elif race == "요괴" and not (5 <= attr_sum <= 18):
        errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "요괴의 속성 합계는 5~18이어야 합니다."))
    return errors

# 캐릭터 심사 결과 저장
async def save_result(character_id, description, pass_status, reason, role_name):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    timestamp = datetime.utcnow().isoformat()
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO results (character_id, description_hash, pass, reason, role_name, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (character_id, description_hash, pass_status, reason, role_name, timestamp))
        await db.commit()

# 캐릭터 심사 결과 조회
async def get_result(description):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT pass, reason, role_name FROM results WHERE description_hash = ?", (description_hash,)) as cursor:
            return await cursor.fetchone()

# Flex 작업 큐에 추가
async def queue_flex_task(character_id, description, user_id, channel_id, thread_id, task_type, prompt):
    task_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT INTO flex_tasks (task_id, character_id, description, user_id, channel_id, thread_id, type, prompt, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, character_id, description, user_id, channel_id, thread_id, task_type, prompt, "pending", created_at))
        await db.commit()
    flex_queue.append(task_id)
    return task_id

# Flex 작업 처리
async def process_flex_queue():
    while True:
        if flex_queue:
            task_id = flex_queue.popleft()
            async with aiosqlite.connect("characters.db") as db:
                async with db.execute("SELECT * FROM flex_tasks WHERE task_id = ?", (task_id,)) as cursor:
                    task = await cursor.fetchone()
                    if not task:
                        continue

                    task_id, character_id, description, user_id, channel_id, thread_id, task_type, prompt, status, created_at = task

                    if status != "pending":
                        continue

                    try:
                        response = openai_client.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=50
                        )
                        result = response.choices[0].message.content.strip()
                        pass_status = result.startswith("✅")
                        role_name = result.split("역할: ")[1] if pass_status else None
                        reason = result[2:] if not pass_status else "통과"

                        await save_result(character_id, description, pass_status, reason, role_name)

                        channel = bot.get_channel(int(channel_id))
                        guild = channel.guild
                        member = guild.get_member(int(user_id))

                        if pass_status:
                            allowed_roles, _ = await get_settings(guild.id)
                            if role_name and role_name not in allowed_roles:
                                result = f"❌ 역할 `{role_name}`은 허용되지 않아! 허용된 역할: {', '.join(allowed_roles)} 🤔"
                            else:
                                has_role = False
                                role = discord.utils.get(guild.roles, name=role_name) if role_name else None
                                if role and role in member.roles:
                                    has_role = True

                                race_role_name = answers.get("종족")
                                race_role = discord.utils.get(guild.roles, name=race_role_name) if race_role_name else None
                                if race_role and race_role in member.roles:
                                    has_role = True

                                if has_role:
                                    result = "🎉 이미 역할이 있어! 마음껏 즐겨~ 🎊"
                                else:
                                    if role:
                                        await member.add_roles(role)
                                        result += f" (역할 `{role_name}` 부여했어! 😊)"
                                    if race_role:
                                        await member.add_roles(race_role)
                                        result += f" (종족 `{race_role_name}` 부여했어! 😊)"

                                # 캐릭터-목록 채널에 게시
                                char_channel = discord.utils.get(guild.channels, name="캐릭터-목록")
                                if char_channel:
                                    await char_channel.send(f"{member.mention}의 캐릭터:\n{description}")
                                else:
                                    result += "\n❌ 캐릭터-목록 채널을 못 찾았어! 🥺"

                        await channel.send(f"{member.mention} {result}")
                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("completed", task_id))
                        await db.commit()

                    except Exception as e:
                        await channel.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")
                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("failed", task_id))
                        await db.commit()
        await asyncio.sleep(1)

# 캐릭터 신청 명령어
answers = {}
@bot.tree.command(name="캐릭터_신청", description="캐릭터를 신청해! 순차적으로 질문에 답해줘~")
async def character_apply(interaction: discord.Interaction):
    await interaction.response.defer()
    global answers
    answers = {}
    user = interaction.user
    channel = interaction.channel

    can_proceed, error_message = await check_cooldown(str(user.id))
    if not can_proceed:
        await interaction.followup.send(error_message)
        return

    await interaction.followup.send("✅ 캐릭터 신청 시작! 질문에 하나씩 답해줘~ 😊")

    for question in questions:
        while True:
            await channel.send(f"{user.mention} {question['prompt']}")
            try:
                response = await bot.wait_for(
                    "message",
                    check=lambda m: m.author == user and m.channel == channel,
                    timeout=300.0
                )
                answer = response.content
                if question["validator"](answer):
                    answers[question["field"]] = answer
                    break
                else:
                    await channel.send(question["error_message"])
            except asyncio.TimeoutError:
                await channel.send(f"{user.mention} ❌ 5분 내로 답변 안 해서 신청 취소됐어! 다시 시도해~ 🥹")
                return

    while True:
        errors = validate_all(answers)
        if not errors:
            break
        fields_to_correct = set()
        error_msg = "다음 문제들이 있어:\n"
        for fields, message in errors:
            error_msg += f"- {message}\n"
            fields_to_correct.update(fields)
        await channel.send(f"{user.mention} {error_msg}다시 입력해줘~")

        for field in fields_to_correct:
            question = next(q for q in questions if q["field"] == field)
            while True:
                await channel.send(f"{user.mention} {field}을 다시 입력해: {question['prompt']}")
                response = await bot.wait_for(
                    "message",
                    check=lambda m: m.author == user and m.channel == channel,
                    timeout=300.0
                )
                answer = response.content
                if question["validator"](answer):
                    answers[field] = answer
                    break
                else:
                    await channel.send(question["error_message"])

    description = "\n".join([f"{field}: {answers[field]}" for field in answers])
    allowed_roles, _ = await get_settings(interaction.guild.id)
    prompt = DEFAULT_PROMPT.format(
        banned_words=', '.join(BANNED_WORDS),
        required_fields=', '.join(REQUIRED_FIELDS),
        allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
        allowed_roles=', '.join(allowed_roles),
        description=description
    )
    await queue_flex_task(str(uuid.uuid4()), description, str(user.id), str(channel.id), None, "character_check", prompt)
    await channel.send(f"{user.mention} ⏳ 심사 중이야! 곧 결과 알려줄게~ 😊")

@bot.event
async def on_ready():
    await init_db()
    print(f'봇이 로그인했어: {bot.user}')
    await bot.tree.sync()
    bot.loop.create_task(process_flex_queue())

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    bot.run(DISCORD_TOKEN)
