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
import logging

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
REQUIRED_FIELDS = ["이름", "나이", "성격"]
LOG_CHANNEL_ID = 1358060156742533231
COOLDOWN_SECONDS = 5
MAX_REQUESTS_PER_DAY = 1000

# 기본 설정값
DEFAULT_ALLOWED_RACES = ["인간", "마법사", "AML", "요괴"]
DEFAULT_ALLOWED_ROLES = ["학생", "선생님", "AML"]
DEFAULT_CHECK_CHANNEL_NAME = "입학-신청서"

# 정규 표현식 (수정됨: 기술 파싱 안정화)
NUMBER_PATTERN = (
    r"\b(체력|지능|이동속도|힘)\s*[:：]\s*([1-6])\b|"  # 속성
    r"\b냉철\s*[:：]\s*([1-4])\b|"  # 냉철
    r"(?:[<\[({【《〈「]([^\]\)>}】》〉」\n]+)[\]\)>}】》〉」])\s*(?:(\d)?)?(?:\s*([^\n]*))?"  # 기술: (불꽃)2 손에서 불 발사
)
AGE_PATTERN = r"\b나이\s*[:：]\s*(\d+)|(?:\b나이\s*[:：](\d+))"
FIELD_PATTERN = r"\b({})\s*[:：]\s*([^\n]+)|(?:\b({})\s*[:：]([^\n]+))"
SKILL_LIST_PATTERN = r"\b사용 기술\/마법\/요력\s*[:：]\s*([\s\S]*?)(?=\n\s*\w+\s*[:：]|\Z)"

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
- 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~6) (이미 확인됨).
- 필드 형식: '필드명: 값', '필드명 : 값', '필드명:값' 등 띄어쓰기 및 콜론(: 또는 :) 허용.
- 기술 표기: <기술명>, [기술명], (기술명), {기술명}, 【기술명】, 《기술명》, 〈기술명〉, 「기술명」.
- 위력 표기: '기술명 1', '기술명 위력 1', '기술명 위력: 1', '기술명 위력 : 1' 등.
- 기술 설명: 같은 줄, 다음 줄, 들여쓰기 유무 상관없이 기술명/위력 뒤 텍스트.
- 필드(이름, 나이, 성격, 과거사 등)와 기술은 구분. 필드는 기술로 오인 금지.
- 설명은 현실적, 역할극 적합.
- 시간/현실 조작 능력 금지.
- 과거사: 시간 여행, 초자연적, 비현실적 사건 금지.
- 나이: 1~5000살 (이미 확인됨).
- 소속: A.M.L, 하람고, 하람고등학교만 허용.
- 속성 합산: 인간 5~16, 마법사 5~17, 요괴 5~18.
- 학년 및 반: 'x-y반', 'x학년 y반', 'x/y반' 형식.
- 기술/마법 위력: 1~5.
- 기술/마법/요력: 시간, 범위, 위력 명확, 과도 금지.
- 기술/마법/요력: 최대 6개.
- AML 소속 시 요괴 불가(정체 숨김 맥락 제외).
- 위력 4~5는 쿨타임/리스크 필수.
- 치유/방어 계열 역계산.
- 정신 계열 능력 불가.
- 스탯표 준수.
- 기술/마법/요력 옆 숫자는 위력.

**역할 판단**:
1. 소속 'AML' 또는 'A.M.L' → AML.
2. 소속 '선생' 또는 '선생님' → 선생님.
3. 소속 '학생' 또는 괄호 학생 → 학생.
4. 미충족 → 실패.

**주의**:
- AML/선생님 조건 시 학생 판단 금지.
- 역할은 {allowed_roles} 중 하나.
- 역할 모호 시 실패.

**설정**:
- 마법 실제 존재.
- 몇 년 전 사건으로 마법/이종족 공개.
- 2050년 미래.
- 마법사/요괴 공존 의사.
- 하람고등학교: 학생/요괴/마법사 공존.
- AML: 하람고 적대, 갈등 조장.

**스탯표**:
지능
1 = IQ 60~80
2 = IQ 90
3 = IQ 100
4 = IQ 120
5 = IQ 150
6 = IQ 180

힘
1 = 1~29kg
2 = 30kg
3 = 50kg
4 = 125kg
5 = 300kg
6 = 600kg

이동속도
1 = 움직임 버거움
2 = 평균보다 느림
3 = 100m 25~20초
4 = 100m 19~13초
5 = 100m 12~6초
6 = 100m 5~3초

냉철
1 = 원초적 감정
2 = 평범한 청소년
3 = 격한 감정 무시
4 = 감정 동요 없음

체력
1 = 간신히 생존
2 = 운동 부족
3 = 평범한 청소년
4 = 운동선수
5 = 초인적 맷집
6 = 인간 한계 초월

능력/마법/기술 위력
1 = 피해 없음
2 = 경미한 상처
3 = 깊은 상처
4 = 불구/사망
5 = 콘크리트 파괴

**캐릭터 설명**:
{description}

**응답 형식**:
- 통과: "✅ 역할: [역할]"
- 실패: "❌ [실패 이유]"
"""

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

# 서버별 설정 저장
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

# 캐릭터 심사 결과 삭제
async def clear_result(description):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("DELETE FROM results WHERE description_hash = ?", (description_hash,))
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
                return False, f"❌ 하루 요청 한도 초과! 최대 {MAX_REQUESTS_PER_DAY}번! 내일 와! 😊"

            if (now - last_request).total_seconds() < COOLDOWN_SECONDS:
                return False, f"❌ {COOLDOWN_SECONDS}초 더 기다려! 잠시 쉬어~ 😅"

            await db.execute("UPDATE cooldowns SET last_request = ?, request_count = ? WHERE user_id = ?",
                             (now.isoformat(), request_count + 1, user_id))
            await db.commit()
            return True, ""

# 캐릭터 설명 검증 (수정됨: 기술 파싱 개선, 예외 처리 강화)
async def validate_character(description):
    logger.info(f"Validating character description: {description[:100]}...")
    if len(description) < MIN_LENGTH:
        return False, f"❌ 설명 너무 짧아! 최소 {MIN_LENGTH}자 써줘~ 📝"

    # 필수 필드 체크
    found_fields = []
    field_values = {}
    for field in REQUIRED_FIELDS:
        pattern = r"\b" + field + r"\s*[:：]\s*([^\n]+)|(?:\b" + field + r"\s*[:：]([^\n]+))"
        match = re.search(pattern, description)
        if match:
            value = match.group(1) or match.group(2)
            found_fields.append(field)
            field_values[field] = value.strip()
    
    missing_fields = [field for field in REQUIRED_FIELDS if field not in found_fields]
    if missing_fields:
        return False, f"❌ {', '.join(missing_fields)} 빠졌어! '{field}: 값' 또는 '{field}:값' 써줘~ 🧐"

    found_banned_words = [word for word in BANNED_WORDS if word in description]
    if found_banned_words:
        return False, f"❌ 금지 단어 {', '.join(found_banned_words)} 포함! 규칙 지켜~ 😅"

    # 나이 검증
    if "나이" in field_values:
        try:
            age = int(field_values["나이"])
            if not (1 <= age <= 5000):
                return False, f"❌ 나이 {age}살? 1~5000살로~ 🕰️"
        except ValueError:
            return False, f"❌ 나이는 숫자! 예: '나이: 30' 또는 '나이:30' 😄"
    else:
        return False, f"❌ 나이 써줘! '나이: 숫자' 또는 '나이:숫자'~ 😄"

    # 기술 및 속성 검증
    matches = re.finditer(NUMBER_PATTERN, description, re.MULTILINE)  # finditer로 매칭 위치 추적
    skill_count = 0
    skills = []
    attributes = {}
    
    for match in matches:
        logger.info(f"NUMBER_PATTERN match: {match.group()} at position {match.start()}-{match.end()}")
        if match.group(1):  # 속성 (체력, 지능, 이동속도, 힘)
            value = int(match.group(2))
            if not (1 <= value <= 6):
                return False, f"❌ '{match.group(1)}' {value}? 1~6으로~ 💪"
            attributes[match.group(1)] = value
        elif match.group(3):  # 냉철
            value = int(match.group(3))
            if not (1 <= value <= 4):
                return False, f"❌ 냉철 {value}? 1~4로~ 🧠"
            attributes["냉철"] = value
        elif match.group(4):  # 기술
            skill_name = match.group(4).strip()
            # 기술명이 필드명과 겹치거나 비어 있는 경우 스킵
            if not skill_name or any(field.lower() in skill_name.lower() for field in REQUIRED_FIELDS + ["소속", "종족", "키/몸무게", "과거사", "사용 기술/마법/요력"]):
                logger.info(f"Skipping skill '{skill_name}' due to invalid name or overlap with field")
                continue
            power = match.group(5)
            skill_desc = match.group(6).strip() if match.group(6) else "기본 기술"
            # 기술 설명이 기술명으로 잘못 파싱되지 않도록 추가 검증
            if skill_desc and any(field.lower() in skill_desc.lower() for field in REQUIRED_FIELDS + ["소속", "종족", "키/몸무게", "과거사", "사용 기술/마법/요력"]):
                logger.info(f"Adjusting skill description for '{skill_name}': {skill_desc} might be a field, setting to default")
                skill_desc = "기본 기술"
            try:
                value = int(power) if power else 1
                if not (1 <= value <= 5):
                    return False, f"❌ '{skill_name}' 위력 {value}? 1~5로~ 🔥"
            except (ValueError, TypeError) as e:
                logger.error(f"Skill power parsing error for '{skill_name}': {str(e)}")
                return False, f"❌ '{skill_name}' 위력 숫자 아님! 예: '({skill_name}) 1' 😅"
            skill_count += 1
            skills.append({"name": skill_name, "power": value, "description": skill_desc})

    # 기술 목록 필드 처리
    skill_list_match = re.search(SKILL_LIST_PATTERN, description)
    if skill_list_match:
        skill_list = skill_list_match.group(1).strip().split("\n")
        for skill_line in skill_list:
            skill_line = skill_line.strip()
            if not skill_line:
                continue
            skill_match = re.match(r"(?:[-*] )?([^\(]+)(?:\s*\(위력\s*[:：]?\s*(\d)\))?(?:\s*([^\n]*))?", skill_line)
            if skill_match:
                skill_name = skill_match.group(1).strip()
                power = skill_match.group(2)
                skill_desc = skill_match.group(3).strip() if skill_match.group(3) else "기본 기술"
                try:
                    value = int(power) if power else 1
                    if not (1 <= value <= 5):
                        return False, f"❌ '{skill_name}' 위력 {value}? 1~5로~ 🔥"
                except (ValueError, TypeError) as e:
                    logger.error(f"Skill list power parsing error for '{skill_name}': {str(e)}")
                    return False, f"❌ '{skill_name}' 위력 숫자 아님! 예: '{skill_name} (위력: 1)' 😅"
                skill_count += 1
                skills.append({"name": skill_name, "power": value, "description": skill_desc})

    if skill_count > 6:
        return False, f"❌ 기술 {skill_count}개? 최대 6개야~ ⚔️"

    logger.info(f"Parsed fields: {field_values}")
    logger.info(f"Parsed attributes: {attributes}")
    logger.info(f"Parsed skills: {skills}")

    return True, ""

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
                            
                            messages = [message async for message in thread.history(limit=1, oldest_first=True)]
                            original_message = messages[0] if messages else None

                            if pass_status and task_type == "character_check":
                                if original_message:
                                    try:
                                        await original_message.add_reaction("☑️")
                                    except discord.Forbidden:
                                        await thread.send("❌ 반응 추가 권한 없어! 🥺")

                                allowed_roles, _ = await get_settings(guild.id)

                                if role_name and role_name not in allowed_roles:
                                    result = f"❌ 역할 `{role_name}` 허용 안 돼! 허용: {', '.join(allowed_roles)} 🤔"
                                else:
                                    has_role = False
                                    role = None
                                    if role_name:
                                        role = discord.utils.get(guild.roles, name=role_name)
                                        if role and role in member.roles:
                                            has_role = True

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

                                    if has_role:
                                        result = "🎉 통과! 역할 이미 있어! 역극 즐겨~ 🎊"
                                    else:
                                        if role:
                                            try:
                                                await member.add_roles(role)
                                                result += f" (`{role_name}` 부여! 😊)"
                                            except discord.Forbidden:
                                                result += f" (`{role_name}` 부여 실패... 권한 없어! 🥺)"
                                        else:
                                            result += f" (`{role_name}` 서버에 없어... 관리자 문의! 🤔)"

                                        if race_role:
                                            try:
                                                await member.add_roles(race_role)
                                                result += f" (종족 `{race_role_name}` 부여! 😊)"
                                            except discord.Forbidden:
                                                result += f" (종족 `{race_role_name}` 부여 실패... 권한 없어! 🥺)"
                                        elif race_role_name:
                                            result += f" (종족 `{race_role_name}` 서버에 없어... 관리자 문의! 🤔)"

                            await thread.send(f"{member.mention} {result}")

                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("completed", task_id))
                        await db.commit()

                        log_channel = bot.get_channel(LOG_CHANNEL_ID)
                        if log_channel:
                            await log_channel.send(f"작업 완료\n유저: {member}\n타입: {task_type}\n결과: {result}")

                    except Exception as e:
                        logger.error(f"Flex queue processing error: {str(e)}")
                        await save_result(character_id, description, False, f"OpenAI 오류: {str(e)}", None) if task_type == "character_check" else None
                        if thread:
                            await thread.send(f"❌ 처리 중 오류: {str(e)} 다시 시도해! 🥹")
                        await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("failed", task_id))
                        await db.commit()
        await asyncio.sleep(1)

# 캐릭터 심사 로직
async def check_character(description, member, guild, thread, force_recheck=False):
    logger.info(f"Checking character for {member.name}: {description[:100]}...")
    try:
        if not force_recheck:
            cached_result = await get_result(description)
            if cached_result:
                pass_status, reason, role_name = cached_result
                if pass_status:
                    allowed_roles, _ = await get_settings(guild.id)
                    if role_name and role_name not in allowed_roles:
                        result = f"❌ 역할 `{role_name}` 허용 안 돼! 허용: {', '.join(allowed_roles)} 🤔"
                    else:
                        has_role = False
                        role = None
                        if role_name:
                            role = discord.utils.get(guild.roles, name=role_name)
                            if role and role in member.roles:
                                has_role = True

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

                        if has_role:
                            result = "🎉 이미 통과! 역할 있어! 역극 즐겨~ 🎊"
                        else:
                            result = f"🎉 이미 통과! 역할: {role_name} 🎊"
                            if role:
                                try:
                                    await member.add_roles(role)
                                    result += f" (`{role_name}` 부여! 😊)"
                                except discord.Forbidden:
                                    result += f" (`{role_name}` 부여 실패... 권한 없어! 🥺)"
                            else:
                                result += f" (`{role_name}` 서버에 없어... 관리자 문의! 🤔)"

                            if race_role:
                                try:
                                    await member.add_roles(race_role)
                                    result += f" (종족 `{race_role_name}` 부여! 😊)"
                                except discord.Forbidden:
                                    result += f" (종족 `{race_role_name}` 부여 실패... 권한 없어! 🥺)"
                            elif race_role_name:
                                result += f" (종족 `{race_role_name}` 서버에 없어... 관리자 문의! 🤔)"
                else:
                    result = f"❌ 이전 실패: {reason} 수정 후 /재검사! 💪"
                return result

        if force_recheck:
            await clear_result(description)

        is_valid, error_message = await validate_character(description)
        if not is_valid:
            await save_result(str(thread.id), description, False, error_message, None)
            return error_message

        allowed_roles, _ = await get_settings(guild.id)
        prompt_template = await get_prompt(guild.id, allowed_roles)
        prompt = prompt_template.format(description=description)

        try:
            await queue_flex_task(str(thread.id), description, str(member.id), str(thread.parent.id), str(thread.id), "character_check", prompt)
            return "⏳ 심사 중! 곧 결과 알려줄게~ 😊"
        except Exception as e:
            logger.error(f"Queue error: {str(e)}")
            await save_result(str(thread.id), description, False, f"큐 오류: {str(e)}", None)
            return f"❌ 심사 요청 오류: {str(e)} 다시 시도해! 🥹"

    except Exception as e:
        logger.error(f"Validation error: {str(e)}")
        await save_result(str(thread.id), description, False, f"검증 오류: {str(e)}", None)
        return f"❌ 검증 오류: {str(e)} 나중에 시도해! 🥹"

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
                found_fields = []
                for field in REQUIRED_FIELDS:
                    if re.search(r"\b" + field + r"\s*[:：]\s*[^\n]+|\b" + field + r"\s*[:：][^\n]+", message.content):
                        found_fields.append(field)
                if len(found_fields) == len(REQUIRED_FIELDS):
                    return message.content
    except discord.Forbidden:
        return None
    return None

@bot.event
async def on_ready():
    await init_db()
    logger.info(f'Bot logged in: {bot.user}')
    await bot.tree.sync()
    bot.loop.create_task(process_flex_queue())

@bot.event
async def on_thread_create(thread):
    logger.info(f"New thread: {thread.name} (parent: {thread.parent.name})")
    _, check_channel_name = await get_settings(thread.guild.id)
    if thread.parent.name == check_channel_name and not thread.owner.bot:
        try:
            bot_member = thread.guild.me
            permissions = thread.permissions_for(bot_member)
            if not permissions.send_messages or not permissions.read_message_history:
                await thread.send("❌ 권한 없어! 관리자 문의~ 🥺")
                return

            messages = [message async for message in thread.history(limit=1, oldest_first=True)]
            if not messages or messages[0].author.bot:
                await thread.send("❌ 첫 메시지 못 찾음! 다시 올려~ 🤔")
                return

            message = messages[0]
            can_proceed, error_message = await check_cooldown(str(message.author.id))
            if not can_proceed:
                await thread.send(f"{message.author.mention} {error_message}")
                return

            result = await check_character(message.content, message.author, message.guild, thread)
            await thread.send(f"{message.author.mention} {result}")

        except Exception as e:
            logger.error(f"Thread creation error: {str(e)}")
            await thread.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"오류: {str(e)}")

# 피드백 명령어
@bot.tree.command(name="피드백", description="심사 결과 질문! 예: /피드백 왜 안된거야?")
async def feedback(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        description = await find_recent_character_description(interaction.channel, interaction.user)
        if not description:
            await interaction.followup.send("❌ 최근 설명 못 찾음! 먼저 올려~ 😊")
            return

        cached_result = await get_result(description)
        if not cached_result:
            await interaction.followup.send("❌ 심사 결과 없음! 먼저 심사해~ 🤔")
            return

        pass_status, reason, role_name = cached_result
        prompt = f"""
        캐릭터 설명: {description}
        심사 결과: {'통과' if pass_status else '실패'}, 이유: {reason}
        질문: {question}
        50자 내 간단 답변. 친근 재밌게. 통과/탈락 여부 먼저.
        """
        task_id = await queue_flex_task(None, description, str(interaction.user.id), str(interaction.channel.id), None, "feedback", prompt)
        await interaction.followup.send("⏳ 피드백 처리 중! 곧 알려줄게~ 😊")

    except Exception as e:
        logger.error(f"Feedback error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 재검사 명령어
@bot.tree.command(name="재검사", description="최근 캐릭터 다시 심사!")
async def recheck(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        description = await find_recent_character_description(interaction.channel, interaction.user)
        if not description:
            await interaction.followup.send("❌ 최근 설명 못 찾음! 먼저 올려~ 😊")
            return

        result = await check_character(description, interaction.user, interaction.guild, interaction.channel, force_recheck=True)
        await interaction.followup.send(f"{interaction.user.mention} {result}")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"재검사 요청\n유저: {interaction.user}\n결과: {result}")

    except Exception as e:
        logger.error(f"Recheck error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 질문 명령어
@bot.tree.command(name="질문", description="QnA 채널 질문! 예: /질문 서버 규칙 뭐야?")
async def ask_question(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 프롬프트 수정 명령어
@bot.tree.command(name="프롬프트_수정", description="관리실에서 프롬프트 수정! 예: /프롬프트_수정 [내용]")
async def modify_prompt(interaction: discord.Interaction, new_prompt: str):
    await interaction.response.defer()
    try:
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 관리실에서만 가능! 😅")
            return

        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        if len(new_prompt) > 2000:
            await interaction.followup.send("❌ 프롬프트 너무 길어! 2000자 내로~ 📝")
            return

        await save_prompt(interaction.guild.id, new_prompt)
        await interaction.followup.send("✅ 프롬프트 수정 완료! 적용됨~ 😊")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"프롬프트 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n프롬프트: {new_prompt[:100]}...")

    except Exception as e:
        logger.error(f"Modify prompt error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 프롬프트 초기화 명령어
@bot.tree.command(name="프롬프트_초기화", description="관리실에서 프롬프트 기본값 초기화!")
async def reset_prompt(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 관리실에서만 가능! 😅")
            return

        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        allowed_roles, _ = await get_settings(interaction.guild.id)
        default_prompt = DEFAULT_PROMPT.format(
            banned_words=', '.join(BANNED_WORDS),
            required_fields=', '.join(REQUIRED_FIELDS),
            allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
            allowed_roles=', '.join(allowed_roles),
            description="{description}"
        )
        await save_prompt(interaction.guild.id, default_prompt)
        await interaction.followup.send("✅ 프롬프트 기본값 초기화! 😊")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"프롬프트 초기화\n서버: {interaction.guild.name}\n유저: {interaction.user}")

    except Exception as e:
        logger.error(f"Reset prompt error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 역할 수정 명령어
@bot.tree.command(name="역할_수정", description="관리실에서 역할 수정! 예: /역할_수정 학생,전사,마법사")
async def modify_roles(interaction: discord.Interaction, roles: str):
    await interaction.response.defer()
    try:
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 관리실에서만 가능! 😅")
            return

        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        new_roles = [role.strip() for role in roles.split(",")]
        if not new_roles:
            await interaction.followup.send("❌ 역할 비어있어! 1개 이상 입력~ 😅")
            return

        await save_settings(interaction.guild.id, allowed_roles=new_roles)
        await interaction.followup.send(f"✅ 역할 수정: {', '.join(new_roles)} 😊")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"역할 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n역할: {', '.join(new_roles)}")

    except Exception as e:
        logger.error(f"Modify roles error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 검사 채널 수정 명령어
@bot.tree.command(name="검사채널_수정", description="관리실에서 검사 채널 수정! 예: /검사채널_수정 캐릭터-심사")
async def modify_check_channel(interaction: discord.Interaction, channel_name: str):
    await interaction.response.defer()
    try:
        if "관리실" not in interaction.channel.name.lower():
            await interaction.followup.send("❌ 관리실에서만 가능! 😅")
            return

        can_proceed, error_message = await check_cooldown(str(interaction.user.id))
        if not can_proceed:
            await interaction.followup.send(error_message)
            return

        if len(channel_name) > 50:
            await interaction.followup.send("❌ 채널 이름 너무 길어! 50자 내로~ 📝")
            return

        await save_settings(interaction.guild.id, check_channel_name=channel_name)
        await interaction.followup.send(f"✅ 검사 채널 수정: `{channel_name}` 😊")

        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"검사 채널 수정\n서버: {interaction.guild.name}\n유저: {interaction.user}\n채널: {channel_name}")

    except Exception as e:
        logger.error(f"Modify check channel error: {str(e)}")
        await interaction.followup.send(f"❌ 오류: {str(e)} 다시 시도~ 🥹")

# 양식 안내 명령어
@bot.tree.command(name="양식_안내", description="캐릭터 양식 예시 확인!")
async def format_guide(interaction: discord.Interaction):
    await interaction.response.defer()
    guide = """
    ✅ 캐릭터 양식 예시:
    - 필드: '이름: 값', '이름 : 값', '이름:값' 가능
    - 기술: <기술명> 1, [기술명] 1, (기술명) 1, {기술명} 1, 【기술명】 1, 《기술명》 1, 〈기술명〉 1, 「기술명」 1
    - 위력: '기술명 1', '기술명 위력 1', '기술명 위력: 1', '기술명 위력 : 1'
    - 기술 목록: '사용 기술/마법/요력: 기술명 (위력: 1) 설명'
    - 기술 설명: 같은 줄 또는 다음 줄 (예: <기술명> 1 설명 또는 \n    설명)
    - 이전 실패 시: '/재검사'로 새 심사 요청!
    예시:
"안녕!!"

이름:다크
성별: 여성
종족:요괴
나이: 230
소속:학생
학년, 반: 3학년1반  
동아리: 

키/몸무게: 172/56 
성격:자유로운 영혼
외모:(사진이 있다면 미기재해도 됩니다)

체력: 6 - 늪 생물채한테 심장과 뇌가 있다고 생각하십니까? 
지능: 4
이동속도: 6-생존을 위해 도망다니기에 최적화 되었다
힘: 2
냉철: 2
사용 기술/마법/요력: 
형태를 이루는 늪 (1)
다크의 몸은 늪으로 이루어져 있습니다. 늪지대가 있는이상 다크의 늪이 25%이상 남아 있어야 회복 됩니다 늪은 다크의 몸을 이루기 때문에 신채 능력과 관련있습니다. 늪이 줄어들수록 다크의 덩치와 대미지가 줄어듭니다
25%미만일때는 꼬마도마뱀으로 변합니다 이때는 지성만 남아있고 능력은 사용이 블가합니다 
회복할때마다 오감이 서서히 사라집니다.

늪(3)
물채를 늪으로 만들어 조종합니다

경질화(3)
늪을 압축시켜 단단하게 만듭니다


과거사:지하에서 탄생한 생명체.
특징:음식을 잘 먹는다

관계: 
    """
    await interaction.followup.send(guide)

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    bot.run(DISCORD_TOKEN)
