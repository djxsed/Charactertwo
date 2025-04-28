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

# 환경 변수 불러오기 (비밀 정보 보호)
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

# 상수 정의 (설정값들)
BANNED_WORDS = ["악마", "천사", "이세계", "드래곤"]
MIN_LENGTH = 50
REQUIRED_FIELDS = ["이름:", "나이:", "성격:"]
LOG_CHANNEL_ID = 1358060156742533231
COOLDOWN_SECONDS = 5
MAX_REQUESTS_PER_DAY = 1000

# 기본 설정값 (DB에 저장되지 않은 경우 사용)
DEFAULT_ALLOWED_RACES = ["인간", "마법사", "A.M.L", "요괴"]
DEFAULT_ALLOWED_ROLES = ["학생", "선생님", "A.M.L"]
DEFAULT_CHECK_CHANNEL_NAME = "입학-신청서"

# 숫자 속성 체크용 정규 표현식
NUMBER_PATTERN = r"\b(체력|지능|이동속도|힘)\s*:\s*([1-6])\b|\b냉철\s*:\s*([1-4])\b|\[\w+\]\s*\((\d)\)"
AGE_PATTERN = r"나이:\s*(\d+)"

# 기본 프롬프트 (서버별 프롬프트가 없을 경우 사용)
DEFAULT_PROMPT = """
디스코드 역할극 서버의 캐릭터 심사 봇이야. 캐릭터 설명을 보고:
1. 서버 규칙에 맞는지 판단해.
2. 캐릭터가 {allowed_roles} 중 하나인지 정해.
**간결하게 50자 이내로 답변해!**

**규칙**:
- 금지 단어: {banned_words} (이미 확인됨).
- 필수 항목: {required_fields} (이미 확인됨).
- 허용 종족: {allowed_races}.
- 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~5) (이미 확인됨).
- 설명은 현실적이고 역할극에 적합해야 해.
- 시간/현실 조작 능력 금지.
- 과거사: 시간 여행, 초자연적 능력, 비현실적 사건(예: 세계 구함) 금지.
- 나이: 1~5000살 (이미 확인됨).
- 소속: A.M.L, 하람고, 하람고등학교만 허용 (동아리 제외).
- 속성 합산(체력, 지능, 이동속도, 힘, 냉철): 인간 5~16, 마법사 5~17, 요괴 5~18.
- 학년 및 반은 'x-y반', 'x학년 y반', 'x/y반' 형식만 인정.
- 기술/마법/요력: 시간, 범위, 위력 등이 명확해야 하고 너무 크면 안 돼. (예: 18초, 50m, 5).

**역할 판단 (이 순서대로 엄격히 확인)**:
1. 소속에 'AML' 또는 'A.M.L'이 포함되면 A.M.L로 판단.
2. 소속에 '선생' 또는 '선생님'이 적혀있다면 선생님으로 판단.
3. 소속에 '학생' 또는 괄호 사이의 학생 등이 적혀있다면 학생으로 판단.
4. 위 조건에 해당되지 않으면 실패.

**주의**:
- A.M.L이나 선생님 조건이 충족되면 학생으로 판단하지 마.
- 역할은 반드시 {allowed_roles} 중 하나만 선택.
- 역할 판단이 모호하면 실패 처리.

**캐릭터 설명**:
{description}

**응답 형식**:
- 통과: "✅ 역할: [역할]"
- 실패: "❌ [실패 이유]"
"""

# Flex 작업 큐
flex_queue = deque()

# 데이터베이스 초기화 (수정: settings 테이블 추가)
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
        # 서버별 설정 저장 테이블 추가 (역할, 검사 채널 이름)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id TEXT PRIMARY KEY,
                allowed_roles TEXT,  -- JSON 형식으로 저장
                check_channel_name TEXT
            )
        """)
        await db.commit()

# 서버별 설정 조회 (추가)
async def get_settings(guild_id):
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT allowed_roles, check_channel_name FROM settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                # allowed_roles는 JSON 형식으로 저장됨 (예: "학생,선생님,A.M.L")
                allowed_roles = row[0].split(",") if row[0] else DEFAULT_ALLOWED_ROLES
                check_channel_name = row[1] if row[1] else DEFAULT_CHECK_CHANNEL_NAME
                return allowed_roles, check_channel_name
            return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME

# 서버별 설정 저장 (추가)
async def save_settings(guild_id, allowed_roles=None, check_channel_name=None):
    current_allowed_roles, current_check_channel_name = await get_settings(guild_id)
    allowed_roles = allowed_roles if allowed_roles is not None else current_allowed_roles
    check_channel_name = check_channel_name if check_channel_name is not None else current_check_channel_name
    
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO settings (guild_id, allowed_roles, check_channel_name)
            VALUES (?, ?, ?)
        """, (str(guild_id), ",".join(allowed_roles), check_channel_name))
        await db.commit()

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

# 서버별 프롬프트 저장
async def save_prompt(guild_id, prompt_content):
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO prompts (guild_id, prompt_content)
            VALUES (?, ?)
        """, (str(guild_id), prompt_content))
        await db.commit()

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

# 캐릭터 심사 결과 조회
async def get_result(description):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    async with aiosqlite.connect("characters.db") as db:
        async with db.execute("SELECT pass, reason, role_name FROM results WHERE description_hash = ?", (description_hash,)) as cursor:
            return await cursor.fetchone()

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
                return False, f"❌ 하루에 너무 많이 요청했어! 최대 {MAX_REQUESTS_PER_DAY}번이야~ 내일 다시 와! 😊"

            if (now - last_request).total_seconds() < COOLDOWN_SECONDS:
                return False, f"❌ 아직 {COOLDOWN_SECONDS}초 더 기다려야 해! 잠시 쉬어~ 😅"

            await db.execute("UPDATE cooldowns SET last_request = ?, request_count = ? WHERE user_id = ?",
                             (now.isoformat(), request_count + 1, user_id))
            await db.commit()
            return True, ""

# 캐릭터 설명 검증
async def validate_character(description):
    if len(description) < MIN_LENGTH:
        return False, f"❌ 설명이 너무 짧아! 최소 {MIN_LENGTH}자는 써줘~ 📝"

    missing_fields = [field for field in REQUIRED_FIELDS if field not in description]
    if missing_fields:
        return False, f"❌ {', '.join(missing_fields)}가 빠졌어! 꼭 넣어줘~ 🧐"

    found_banned_words = [word for word in BANNED_WORDS if word in description]
    if found_banned_words:
        return False, f"❌ 금지된 단어 {', '.join(found_banned_words)}가 있어! 규칙 지켜줘~ 😅"

    age_match = re.search(AGE_PATTERN, description)
    if age_match:
        age = int(age_match.group(1))
        if not (1 <= age <= 5000):
            return False, f"❌ 나이가 {age}살이야? 1~5000살 사이로 해줘~ 🕰️"
    else:
        return False, "❌ 나이를 '나이: 숫자'로 써줘! 궁금해~ 😄"

    matches = re.findall(NUMBER_PATTERN, description)
    for match in matches:
        if match[1]:
            value = int(match[1])
            if not (1 <= value <= 6):
                return False, f"❌ '{match[0]}'이 {value}야? 1~6으로 해줘~ 💪"
        elif match[2]:
            value = int(match[2])
            if not (1 <= value <= 4):
                return False, f"❌ 냉철이 {value}? 1~4로 해줘~ 🧠"
        elif match[3]:
            value = int(match[3])
            if not (1 <= value <= 5):
                return False, f"❌ 기술/마법 위력이 {value}? 1~5로 해줘~ 🔥"

    return True, ""

# Flex 작업 처리 (수정: 동적으로 허용된 역할 조회)
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
                            model="gpt-4.1-nano",
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=50 
                        )
                        result = response.choices[0].message.content.strip()
                        pass_status = result.startswith("✅")
                        role_name = result.split("역할: ")[1] if pass_status else None
                        reason = result[2:] if not pass_status else "통과"

                        await save_result(character_id, description, pass_status, reason, role_name)

                        thread = bot.get_channel(int(thread_id)) if thread_id else bot.get_channel(int(channel_id))
                        if thread:
                            guild = thread.guild if hasattr(thread, 'guild') else thread
                            member = guild.get_member(int(user_id))
                            if pass_status and task_type == "character_check":  # 캐릭터 심사일 때만 역할 부여
                                # 체크 이모티콘 추가
                                await thread.send("✅")  # 통과 시 스레드에 체크 이모티콘 표시

                                # 서버별 허용된 역할 조회
                                allowed_roles, _ = await get_settings(guild.id)

                                # 역할 확인 (허용된 역할 중 하나인지 확인)
                                if role_name and role_name not in allowed_roles:
                                    result = f"❌ 역할 `{role_name}`은 허용되지 않아! 허용된 역할: {', '.join(allowed_roles)} 🤔"
                                else:
                                    # 역할 확인 (학생/선생님/A.M.L 등)
                                    has_role = False
                                    role = None
                                    if role_name:
                                        role = discord.utils.get(guild.roles, name=role_name)
                                        if role and role in member.roles:
                                            has_role = True

                                    # 종족 역할 확인 (인간/마법사/요괴)
                                    race_role_name = None
                                    race_role = None
                                    if "인간" in description:
                                        race_role_name = "인간"
                                    elif "마법사" in description:
                                        race_role_name = "마법사"
                                    elif "요괴" in description:
                                        race_role_name = "요괴"

                                    if race_role_name:
                                        race_role = discord.utils.get(guild.roles, name=race_role_name)
                                        if race_role and race_role in member.roles:
                                            has_role = True

                                    # 이미 역할이 있는 경우 메시지만 표시
                                    if has_role:
                                        result = "🎉 이미 통과된 캐릭터야~ 역할은 이미 있어! 🎊"
                                    else:
                                        # 기존 역할 부여
                                        if role:
                                            try:
                                                await member.add_roles(role)
                                                result += f" (역할 `{role_name}` 부여했어! 😊)"
                                            except discord.Forbidden:
                                                result += f" (역할 `{role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                                        else:
                                            result += f" (역할 `{role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

                                        # 종족 기반 역할 부여 (인간/마법사/요괴)
                                        if race_role:
                                            try:
                                                await member.add_roles(race_role)
                                                result += f" (종족 역할 `{race_role_name}` 부여했어! 😊)"
                                            except discord.Forbidden:
                                                result += f" (종족 역할 `{race_role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                                        elif race_role_name:
                                            result += f" (종족 역할 `{race_role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

                            await thread.send(f"{member.mention} {result}")

                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("completed", task_id))
                        await db.commit()

                        log_channel = bot.get_channel(LOG_CHANNEL_ID)
                        if log_channel:
                            await log_channel.send(f"작업 완료\n유저: {member}\n타입: {task_type}\n결과: {result}")

                    except Exception as e:
                        await save_result(character_id, description, False, f"OpenAI 오류: {str(e)}", None) if task_type == "character_check" else None
                        if thread:
                            await thread.send(f"❌ 앗, 처리 중 오류가 났어... {str(e)} 다시 시도해봐! 🥹")
                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("failed", task_id))
                        await db.commit()
        await asyncio.sleep(1)

# 캐릭터 심사 로직 (수정: 서버별 설정 조회)
async def check_character(description, member, guild, thread):
    print(f"캐릭터 검사 시작: {member.name}")
    try:
        cached_result = await get_result(description)
        if cached_result:
            pass_status, reason, role_name = cached_result
            if pass_status:
                # 서버별 허용된 역할 조회
                allowed_roles, _ = await get_settings(guild.id)

                # 역할 확인
                if role_name and role_name not in allowed_roles:
                    result = f"❌ 역할 `{role_name}`은 허용되지 않아! 허용된 역할: {', '.join(allowed_roles)} 🤔"
                else:
                    # 역할 확인 (학생/선생님/A.M.L 등)
                    has_role = False
                    role = None
                    if role_name:
                        role = discord.utils.get(guild.roles, name=role_name)
                        if role and role in member.roles:
                            has_role = True

                    # 종족 역할 확인 (인간/마법사/요괴)
                    race_role_name = None
                    race_role = None
                    if "인간" in description:
                        race_role_name = "인간"
                    elif "마법사" in description:
                        race_role_name = "마법사"
                    elif "요괴" in description:
                        race_role_name = "요괴"

                    if race_role_name:
                        race_role = discord.utils.get(guild.roles, name=race_role_name)
                        if race_role and race_role in member.roles:
                            has_role = True

                    # 이미 역할이 있는 경우 메시지만 표시
                    if has_role:
                        result = "🎉 이미 통과된 캐릭터야~ 역할은 이미 있어! 🎊"
                    else:
                        result = f"🎉 이미 통과된 캐릭터야~ 역할: {role_name} 🎊"
                        # 기존 역할 부여
                        if role:
                            try:
                                await member.add_roles(role)
                                result += f" (역할 `{role_name}` 부여했어! 😊)"
                            except discord.Forbidden:
                                result += f" (역할 `{role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                        else:
                            result += f" (역할 `{role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

                        # 종족 기반 역할 부여 (인간/마법사/요괴)
                        if race_role:
                            try:
                                await member.add_roles(race_role)
                                result += f" (종족 역할 `{race_role_name}` 부여했어! 😊)"
                            except discord.Forbidden:
                                result += f" (종족 역할 `{race_role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                        elif race_role_name:
                            result += f" (종족 역할 `{race_role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

            else:
                result = f"❌ 이전에 실패했어... 이유: {reason} 다시 수정해봐! 💪"
            return result

        is_valid, error_message = await validate_character(description)
        if not is_valid:
            await save_result(str(thread.id), description, False, error_message, None)
            return error_message

        # 서버별 설정 조회
        allowed_roles, _ = await get_settings(guild.id)
        prompt_template = await get_prompt(guild.id, allowed_roles)
        prompt = prompt_template.format(description=description)

        try:
            await queue_flex_task(str(thread.id), description, str(member.id), str(thread.parent.id), str(thread.id), "character_check", prompt)
            return "⏳ 캐릭터 심사 중이야! 곧 결과 알려줄게~ 😊"
        except Exception as e:
            await save_result(str(thread.id), description, False, f"큐 오류: {str(e)}", None)
            return f"❌ 앗, 심사 요청 중 오류가 났어... {str(e)} 다시 시도해봐! 🥹"

    except Exception as e:
        await save_result(str(thread.id), description, False, f"오류: {str(e)}", None)
        return f"❌ 앗, 오류가 났어... {str(e)} 나중에 다시 시도해! 🥹"

# 최근 캐릭터 설명 찾기
async def find_recent_character_description(channel, user):
    if isinstance(channel, discord.Thread):
        try:
            messages = [message async for message in channel.history(limit=1, oldest_first=True)]
            if messages and messages[0].author == user and not messages[0].content.startswith("/"):
                return messages[0].content
        except discord.Forbidden:
            return None
        channel = channel.parent

    try:
        async for message in channel.history(limit=100):
            if message.author == user and not message.content.startswith("/") and len(message.content) >= MIN_LENGTH:
                if all(field in message.content for field in REQUIRED_FIELDS):
                    return message.content
    except discord.Forbidden:
        return None
    return None

@bot.event
async def on_ready():
    await init_db()
    print(f'봇이 로그인했어: {bot.user}')
    await bot.tree.sync()
    bot.loop.create_task(process_flex_queue())

@bot.event
async def on_thread_create(thread):
    print(f"새 스레드: {thread.name} (부모: {thread.parent.name})")
    # 서버별 검사 채널 이름 조회
    _, check_channel_name = await get_settings(thread.guild.id)
    if thread.parent.name == check_channel_name and not thread.owner.bot:
        try:
            bot_member = thread.guild.me
            permissions = thread.permissions_for(bot_member)
            if not permissions.send_messages or not permissions.read_message_history:
                await thread.send("❌ 권한이 없어! 서버 관리자한테 물어봐~ 🥺")
                return

            messages = [message async for message in thread.history(limit=1, oldest_first=True)]
            if not messages or messages[0].author.bot:
                await thread.send("❌ 첫 메시지를 못 찾았어! 다시 올려줘~ 🤔")
                return

            message = messages[0]
            can_proceed, error_message = await check_cooldown(str(message.author.id))
            if not can_proceed:
                await thread.send(f"{message.author.mention} {error_message}")
                return

            result = await check_character(message.content, message.author, message.guild, thread)
            await thread.send(f"{message.author.mention} {result}")

        except Exception as e:
            await thread.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"오류: {str(e)}")

# 피드백 명령어
@bot.tree.command(name="피드백", description="심사 결과에 대해 질문해! 예: /피드백 왜 안된거야?")
async def feedback(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        description = await find_recent_character_description(interaction.channel, interaction.user)
        if not description:
            await interaction.followup.send("❌ 최근 캐릭터 설명을 못 찾았어! 먼저 올려줘~ 😊")
            return

        cached_result = await get_result(description)
        if not cached_result:
            await interaction.followup.send("❌ 심사 결과를 못 찾았어! 먼저 심사해줘~ 🤔")
            return

        pass_status, reason, role_name = cached_result
        prompt = f"""
        캐릭터 설명: {description}
        심사 결과: {'통과' if pass_status else '실패'}, 이유: {reason}
        사용자 질문: {question}
        50자 이내로 간단히 답변해. 말투는 친근하고 재밌게.
        통과인지 탈락인지 여부부터 설명.
        """
        task_id = await queue_flex_task(None, description, str(interaction.user.id), str(interaction.channel.id), None, "feedback", prompt)
        await interaction.followup.send("⏳ 피드백 처리 중이야! 곧 알려줄게~ 😊")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# 재검사 명령어
@bot.tree.command(name="재검사", description="최근 캐릭터로 다시 심사해!")
async def recheck(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        description = await find_recent_character_description(interaction.channel, interaction.user)
        if not description:
            await interaction.followup.send("❌ 최근 캐릭터 설명을 못 찾았어! 먼저 올려줘~ 😊")
            return

        result = await check_character(description, interaction.user, interaction.guild, interaction.channel)
        await interaction.followup.send(f"{interaction.user.mention} {result}")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"재검사 요청\n유저: {interaction.user}\n결과: {result}")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# 질문 명령어
@bot.tree.command(name="질문", description="QnA 채널에서 질문해! 예: /질문 이 서버 규칙이 뭐야?")
async def ask_question(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        # QnA 채널인지 확인 (채널 이름으로 간단히 판단)
        if "qna" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 이 명령어는 QnA 채널에서만 사용할 수 있어! 😅")
            return

        prompt = f"""
        디스코드 역할극 서버의 도우미 봇이야. 사용자가 질문을 했어.
        질문: {question}
        서버 규칙과 관련된 질문이면 규칙을 간단히 설명하고, 그 외의 질문은 서버와 관련된 재밌는 답변을 줘.
        50자 이내로 간단히 답변해. 말투는 친근하고 재밌게!
        **규칙**:
        - 금지 단어: {', '.join(BANNED_WORDS)}.
        - 필수 항목: {', '.join(REQUIRED_FIELDS)}.
        - 허용 종족: {', '.join(DEFAULT_ALLOWED_RACES)}.
        - 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~5).
        - 나이: 1~5000살.
        - 소속: A.M.L, 하람고, 하람고등학교만 허용.
        """
        task_id = await queue_flex_task(None, None, str(interaction.user.id), str(interaction.channel.id), None, "question", prompt)
        await interaction.followup.send("⏳ 질문 처리 중이야! 곧 답변해줄게~ 😊")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# 프롬프트 수정 명령어 (수정: 관리실 채널에서만 동작)
@bot.tree.command(name="프롬프트_수정", description="관리실 채널에서 프롬프트 수정해! 예: /프롬프트_수정 [새 프롬프트 내용]")
async def modify_prompt(interaction: discord.Interaction, new_prompt: str):
    await interaction.response.defer()
    try:
        # 관리실 채널인지 확인
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 이 명령어는 '관리실' 채널에서만 사용할 수 있어! 😅")
            return

        # 쿨다운 확인
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        # 프롬프트 길이 제한 (최대 2000자로 제한)
        if len(new_prompt) > 2000:
            await interaction.followup.send("❌ 프롬프트가 너무 길어! 2000자 이내로 줄여줘~ 📝")
            return

        # 프롬프트 저장
        await save_prompt(interaction.guild.id, new_prompt)
        await interaction.followup.send("✅ 프롬프트가 성공적으로 수정되었어! 이제 적용될 거야~ 😊")

        # 로그 채널에 기록
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"프롬프트 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n새 프롬프트: {new_prompt[:100]}...")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# 역할 수정 명령어 (추가)
@bot.tree.command(name="역할_수정", description="관리실 채널에서 허용된 역할 수정해! 예: /역할_수정 학생,전사,마법사")
async def modify_roles(interaction: discord.Interaction, roles: str):
    await interaction.response.defer()
    try:
        # 관리실 채널인지 확인
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 이 명령어는 '관리실' 채널에서만 사용할 수 있어! 😅")
            return

        # 쿨다운 확인
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        # 역할 목록 파싱 (쉼표로 구분)
        new_roles = [role.strip() for role in roles.split(",")]
        if not new_roles:
            await interaction.followup.send("❌ 역할 목록이 비어있어! 최소 1개 이상 입력해줘~ 😅")
            return

        # 역할 목록 저장
        await save_settings(interaction.guild.id, allowed_roles=new_roles)
        await interaction.followup.send(f"✅ 허용된 역할이 수정되었어! 새로운 역할: {', '.join(new_roles)} 😊")

        # 로그 채널에 기록
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"역할 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n새 역할: {', '.join(new_roles)}")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# 검사 채널 수정 명령어 (추가)
@bot.tree.command(name="검사채널_수정", description="관리실 채널에서 검사 채널 이름 수정해! 예: /검사채널_수정 캐릭터-심사")
async def modify_check_channel(interaction: discord.Interaction, channel_name: str):
    await interaction.response.defer()
    try:
        # 관리실 채널인지 확인
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 이 명령어는 '관리실' 채널에서만 사용할 수 있어! 😅")
            return

        # 쿨다운 확인
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        # 채널 이름 길이 제한 (최대 50자로 제한)
        if len(channel_name) > 50:
            await interaction.followup.send("❌ 채널 이름이 너무 길어! 50자 이내로 줄여줘~ 📝")
            return

        # 검사 채널 이름 저장
        await save_settings(interaction.guild.id, check_channel_name=channel_name)
        await interaction.followup.send(f"✅ 검사 채널 이름이 수정되었어! 새로운 채널 이름: `{channel_name}` 😊")

        # 로그 채널에 기록
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"검사 채널 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n새 채널 이름: {channel_name}")

    except Exception as e:
        await interaction.followup.send(f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")

# Flask와 디스코드 봇을 동시에 실행
if __name__ == "__main__":
    # Flask 서버를 백그라운드에서 실행
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    # 디스코드 봇 실행
    bot.run(DISCORD_TOKEN)
