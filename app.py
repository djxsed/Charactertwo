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
import time
import aiohttp
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
DB_PATH = os.getenv("DB_PATH", "/opt/render/project/src/bot.db")  # Render에서 쓰기 가능한 경로

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
RATE_LIMIT_DELAY = 1.5  # 기본 지연 시간
DB_MAX_RETRIES = 3
DB_RETRY_DELAY = 2
API_CALL_MIN_INTERVAL = 0.1  # API 호출 간 최소 간격 (초)

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
- 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~6) (이미 확인됨).
- 설명은 역할극에 적합해야 하며, 간단한 일상적 배경도 허용.
- 시간/현실 조작 능력 금지.
- 과거사: 시간 여행, 초자연적 능력(마법 제외), 비현실적 사건(예: 세계 구함, 우주 정복) 금지. 최소 20자면 충분히 구체적이며, 간단한 배경(예: 학교 입학, 가정사)도 통과.
- 나이: 1~5000살 (이미 확인됨).
- 소속: A.M.L, 하람고, 하람고등학교만 허용.
- 속성 합산(체력, 지능, 이동속도, 힘, 냉철): 인간 5~18, 마법사 5~19, 요괴 5~20.
- 학년 및 반은 'x-y반', 'x학년 y반', 'x/y반' 형식만 인정.
- 기술/마법/요력 위력은 1~6만 허용.
- 기술/마법/요력은 시간, 범위, 위력 등이 명확해야 함.
- 기술/마법/요력 개수는 6개 이하.

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
    {"field": "포스트 이름", "prompt": "포스트 이름을 입력해주세요.", "validator": lambda x: len(x) > 0, "error_message": "포스트 이름을 입력해주세요."},
    {"field": "종족", "prompt": "종족을 선택해주세요.", "options": ["인간", "마법사", "요괴"], "error_message": "허용되지 않은 종족입니다. 인간, 마법사, 요괴 중에서 선택해주세요."},
    {"field": "이름", "prompt": "캐릭터의 이름을 입력해주세요.", "validator": lambda x: len(x) > 0, "error_message": "이름을 입력해주세요."},
    {"field": "성별", "prompt": "성별을 선택해주세요.", "options": ["남", "여", "불명"], "error_message": "허용되지 않은 성별입니다. 남, 여, 불명 중에서 선택해주세요."},
    {"field": "나이", "prompt": "나이를 입력해주세요. (1~5000)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 5000, "error_message": "나이는 1에서 5000 사이의 숫자여야 합니다."},
    {"field": "키/몸무게", "prompt": "키와 몸무게를 입력해주세요. (예: 170cm/60kg)", "validator": lambda x: True, "error_message": ""},
    {"field": "성격", "prompt": "성격을 설명해주세요. (최소 10자)", "validator": lambda x: len(x) >= 10, "error_message": "성격 설명이 너무 짧습니다. 최소 10자 이상 입력해주세요."},
    {"field": "외모", "prompt": "외모를 설명(최소 20자)하거나 이미지를 업로드해주세요.", "validator": lambda x: (len(x) >= 20 if isinstance(x, str) and not x.startswith("이미지_") else True), "error_message": "외모 설명이 너무 짧습니다. 최소 20자 이상 입력하거나 이미지를 업로드해주세요."},
    {"field": "소속", "prompt": "소속을 선택해주세요.", "options": ["학생", "선생님", "A.M.L"], "error_message": "허용되지 않은 소속입니다. 학생, 선생님, A.M.L 중에서 선택해주세요."},
    {"field": "학년 및 반", "prompt": "학년과 반을 입력해줘. (예: 1학년 2반, 1-2반, 1/2반)", "validator": lambda x: re.match(r"^\d[-/]\d반$|^\d학년\s*\d반$", x), "error_message": "학년과 반은 'x-y반', 'x학년 y반', 'x/y반' 형식으로 입력해줘.", "condition": lambda answers: answers.get("소속") == "학생"},
    {"field": "담당 과목 및 학년, 반", "prompt": "담당 과목과 학년, 반을 입력해줘. (예: 수학, 1학년 2반)", "validator": lambda x: len(x) > 0, "error_message": "담당 과목과 학년, 반을 입력해줘.", "condition": lambda answers: answers.get("소속") == "선생님"},
    {"field": "체력", "prompt": "체력 수치를 입력해줘. (1~6)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6, "error_message": "체력은 1에서 6 사이의 숫자여야 해."},
    {"field": "지능", "prompt": "지능 수치를 입력해줘. (1~6)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6, "error_message": "지능은 1에서 6 사이의 숫자여야 해."},
    {"field": "이동속도", "prompt": "이동속도 수치를 입력해줘. (1~6)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6, "error_message": "이동속도는 1에서 6 사이의 숫자여야 해."},
    {"field": "힘", "prompt": "힘 수치를 입력해줘. (1~6)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6, "error_message": "힘은 1에서 6 사이의 숫자여야 해."},
    {"field": "냉철", "prompt": "냉철 수치를 입력해줘. (1~4)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 4, "error_message": "냉철은 1에서 4 사이의 숫자여야 해."},
    {"field": "사용 기술/마법/요력", "prompt": "사용 기술/마법/요력을 입력해줘.", "validator": lambda x: len(x) > 0, "error_message": "사용 기술/마법/요력을 입력해줘.", "is_tech": True},
    {"field": "사용 기술/마법/요력 위력", "prompt": "사용 기술/마법/요력의 위력을 입력해줘. (1~6)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 6, "error_message": "위력은 1에서 6 사이의 숫자여야 해.", "is_tech": True},
    {"field": "사용 기술/마법/요력 설명", "prompt": "사용 기술/마법/요력을 설명해줘. (최소 20자)", "validator": lambda x: len(x) >= 20, "error_message": "설명이 너무 짧아. 최소 20자 이상 입력해줘.", "is_tech": True},
    {"field": "사용 기술/마법/요력 추가 여부", "prompt": "기술/마법/요력을 추가할래? (예/아니요)", "validator": lambda x: x in ["예", "아니요"], "error_message": "예 또는 아니요로 입력해줘."},
    {"field": "과거사", "prompt": "과거사를 설명해줘. (최소 20자)", "validator": lambda x: len(x) >= 20, "error_message": "과거사 설명이 너무 짧아. 최소 20자 이상 입력해줘."},
    {"field": "특징", "prompt": "특징을 설명해줘. (최소 10자)", "validator": lambda x: len(x) >= 10, "error_message": "특징 설명이 너무 짧아. 최소 10자 이상 입력해줘."},
    {"field": "관계", "prompt": "관계를 설명해줘. (없으면 '없음' 입력)", "validator": lambda x: True, "error_message": ""},
]

# 수정 가능한 항목 목록
EDITABLE_FIELDS = [q["field"] for q in questions if q["field"] != "사용 기술/마법/요력 추가 여부"]

# Flex 작업 큐
flex_queue = deque()

# 글로벌 속도 제한 관리자
last_api_call = 0
api_lock = asyncio.Lock()

async def rate_limit_api_call():
    """API 호출 간 최소 간격을 보장"""
    global last_api_call
    async with api_lock:
        now = time.time()
        time_since_last = now - last_api_call
        if time_since_last < API_CALL_MIN_INTERVAL:
            await asyncio.sleep(API_CALL_MIN_INTERVAL - time_since_last)
        last_api_call = time.time()

# 데이터베이스 초기화
async def init_db():
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS results (
                        character_id TEXT PRIMARY KEY,
                        description_hash TEXT,
                        pass BOOLEAN,
                        reason TEXT,
                        role_name TEXT,
                        user_id TEXT,
                        character_name TEXT,
                        race TEXT,
                        age TEXT,
                        gender TEXT,
                        thread_id TEXT,
                        description TEXT,
                        timestamp TEXT,
                        post_name TEXT
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
                logger.info("데이터베이스 테이블 생성 완료!")
                return
        except Exception as e:
            logger.error(f"데이터베이스 초기화 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("데이터베이스 초기화 실패. 봇 시작 중단.")
    raise Exception("데이터베이스 초기화 실패")

# 서버별 설정 조회
async def get_settings(guild_id):
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT allowed_roles, check_channel_name FROM settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        allowed_roles = row[0].split(",") if row[0] else DEFAULT_ALLOWED_ROLES
                        check_channel_name = row[1] if row[1] else DEFAULT_CHECK_CHANNEL_NAME
                        return allowed_roles, check_channel_name
                    return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME
        except Exception as e:
            logger.error(f"설정 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("설정 조회 실패.")
    return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME

# 서버별 프롬프트 조회
async def get_prompt(guild_id, allowed_roles):
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
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
        except Exception as e:
            logger.error(f"프롬프트 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("프롬프트 조회 실패.")
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
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
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
        except Exception as e:
            logger.error(f"쿨다운 체크 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("쿨다운 체크 실패.")
    return False, "❌ 서버 오류야! 잠시 후 다시 시도해~ 🥹"

# 추가 검증 함수
def validate_all(answers):
    errors = []
    race = answers.get("종족")
    attributes = [int(answers.get(attr, 0)) for attr in ["체력", "지능", "이동속도", "힘", "냉철"] if answers.get(attr)]
    if len(attributes) == 5:
        attr_sum = sum(attributes)
        if race == "인간" and not (5 <= attr_sum <= 18):
            errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "인간의 속성 합계는 5~18이어야 합니다."))
        elif race == "마법사" and not (5 <= attr_sum <= 19):
            errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "마법사의 속성 합계는 5~19이어야 합니다."))
        elif race == "요괴" and not (5 <= attr_sum <= 20):
            errors.append((["체력", "지능", "이동속도", "힘", "냉철"], "요괴의 속성 합계는 5~20이어야 합니다."))
    
    tech_count = sum(1 for field in answers if field.startswith("사용 기술/마법/요력_"))
    if tech_count > 6:
        errors.append((["사용 기술/마법/요력"], f"기술/마법/요력은 최대 6개까지 가능합니다. 현재 {tech_count}개."))
    
    return errors

# 캐릭터 심사 결과 저장
async def save_result(character_id, description, pass_status, reason, role_name, user_id, character_name, race, age, gender, thread_id, post_name):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    timestamp = datetime.utcnow().isoformat()
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO results (character_id, description_hash, pass, reason, role_name, user_id, character_name, race, age, gender, thread_id, description, timestamp, post_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (character_id, description_hash, pass_status, reason, role_name, user_id, character_name, race, age, gender, thread_id, description, timestamp, post_name))
                await db.commit()
                return
        except Exception as e:
            logger.error(f"심사 결과 저장 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("심사 결과 저장 실패.")

# 캐릭터 심사 결과 조회
async def get_result(description):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT pass, reason, role_name FROM results WHERE description_hash = ?", (description_hash,)) as cursor:
                    return await cursor.fetchone()
        except Exception as e:
            logger.error(f"심사 결과 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("심사 결과 조회 실패.")
    return None

# 사용자별 캐릭터 조회 (대소문자 구분 없이)
async def find_characters_by_post_name(post_name, user_id):
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT character_id, character_name, race, age, gender, thread_id, post_name FROM results WHERE LOWER(post_name) = LOWER(?) AND user_id = ? AND pass = 1", (post_name, user_id)) as cursor:
                    return await cursor.fetchall()
        except Exception as e:
            logger.error(f"캐릭터 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("캐릭터 조회 실패.")
    return []

# 캐릭터 정보 조회
async def get_character_info(character_id):
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT description FROM results WHERE character_id = ?", (character_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        desc = row[0]
                        answers = {}
                        for line in desc.split("\n"):
                            if ": " in line:
                                key, value = line.split(": ", 1)
                                answers[key] = value
                        return answers
                    return None
        except Exception as e:
            logger.error(f"캐릭터 정보 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("캐릭터 정보 조회 실패.")
    return None

# Flex 작업 큐에 추가
async def queue_flex_task(character_id, description, user_id, channel_id, thread_id, task_type, prompt):
    task_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    for attempt in range(DB_MAX_RETRIES):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO flex_tasks (task_id, character_id, description, user_id, channel_id, thread_id, type, prompt, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (task_id, character_id, description, user_id, channel_id, thread_id, task_type, prompt, "pending", created_at))
                await db.commit()
                flex_queue.append(task_id)
                return task_id
        except Exception as e:
            logger.error(f"Flex 작업 추가 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
            if attempt < DB_MAX_RETRIES - 1:
                await asyncio.sleep(DB_RETRY_DELAY)
    logger.error("Flex 작업 추가 실패.")
    return None

# 429 에러 및 기타 HTTP 오류 재시도 로직
async def send_message_with_retry(channel, content, answers=None, post_name=None, max_retries=3, is_interaction=False, interaction=None, files=None, view=None, ephemeral=False):
    files = files or []
    for attempt in range(max_retries):
        try:
            await rate_limit_api_call()  # API 호출 간 간격 보장
            if is_interaction and interaction:
                await interaction.followup.send(content, files=files, view=view, ephemeral=ephemeral)
                logger.info(f"Interaction followup sent: content={content[:50]}..., ephemeral={ephemeral}")
                return None, None
            elif isinstance(channel, discord.ForumChannel) and answers:
                thread_name = f"캐릭터: {post_name}"
                thread = await channel.create_thread(
                    name=thread_name,
                    content=content,
                    auto_archive_duration=10080,
                    files=files
                )
                thread_id = str(thread.thread.id) if hasattr(thread, 'thread') else str(thread.id)
                logger.info(f"Thread created: thread_name={thread_name}, thread_id={thread_id}")
                return thread, thread_id
            else:
                await channel.send(content, files=files, view=view)
                logger.info(f"Message sent: content={content[:50]}...")
                return None, None
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = e.retry_after if hasattr(e, 'retry_after') else 5
                logger.warning(f"429 에러 발생, 재시도 {attempt + 1}/{max_retries}, 대기 시간: {retry_after}초, endpoint: {e.__dict__.get('url', 'unknown')}")
                await asyncio.sleep(retry_after + RATE_LIMIT_DELAY)
            else:
                logger.error(f"HTTP 오류 발생: status={e.status}, message={e.text}, endpoint: {e.__dict__.get('url', 'unknown')}")
                raise e
        except Exception as e:
            logger.error(f"메시지 전송 중 예상치 못한 오류: {e}, endpoint: unknown, content={content[:50]}..., is_interaction={is_interaction}, ephemeral={ephemeral}")
            raise e
    logger.error(f"최대 재시도 횟수 초과: content={content[:50]}..., is_interaction={is_interaction}, ephemeral={ephemeral}")
    raise discord.HTTPException(response=None, message="최대 재시도 횟수 초과")

# 이미지 다운로드 함수
async def download_image(image_url):
    try:
        await rate_limit_api_call()  # API 호출 간 간격 보장
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status == 200:
                    content = await response.read()
                    logger.info(f"이미지 다운로드 성공: url={image_url}")
                    return discord.File(fp=content, filename="appearance.png")
                else:
                    logger.warning(f"이미지 다운로드 실패: url={image_url}, 상태 코드: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"이미지 다운로드 중 에러: url={image_url}, 에러: {e}")
        return None

# Flex 작업 처리
async def process_flex_queue():
    while True:
        if flex_queue:
            task_id = flex_queue.popleft()
            for attempt in range(DB_MAX_RETRIES):
                try:
                    :flutter
                    
System message continued from previous context:

try:
                    async with aiosqlite.connect(DB_PATH) as db:
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

                                answers = {}
                                for line in description.split("\n"):
                                    if ": " in line:
                                        key, value = line.split(": ", 1)
                                        answers[key] = value
                                character_name = answers.get("이름")
                                race = answers.get("종족")
                                age = answers.get("나이")
                                gender = answers.get("성별")
                                post_name = answers.get("포스트 이름")

                                channel = bot.get_channel(int(channel_id))
                                if not channel:
                                    logger.error(f"채널을 찾을 수 없음: {channel_id}")
                                    continue
                                guild = channel.guild
                                member = guild.get_member(int(user_id))
                                if not member:
                                    logger.error(f"멤버를 찾을 수 없음: {user_id}")
                                    continue

                                files = []
                                if answers.get("외모", "").startswith("이미지_"):
                                    image_url = answers["외모"].replace("이미지_", "")
                                    file = await download_image(image_url)
                                    if file:
                                        files.append(file)
                                    else:
                                        logger.warning(f"이미지 다운로드 실패, 파일 없이 진행: {image_url}")

                                if pass_status:
                                    allowed_roles, _ = await get_settings(guild.id)
                                    if role_name and role_name not in allowed_roles:
                                        result = f"❌ 역할 `{role_name}`은 허용되지 않아! 허용된 역할: {', '.join(allowed_roles)} 🤔"
                                    else:
                                        has_role = False
                                        role = discord.utils.get(guild.roles, name=role_name) if role_name else None
                                        race_role = discord.utils.get(guild.roles, name=race) if race else None
                                        if role and role in member.roles:
                                            has_role = True
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
                                                result += f" (종족 `{race}` 부여했어! 😊)"

                                        # 출력 양식
                                        formatted_description = (
                                            f"이름: {answers.get('이름', '미기재')}\n"
                                            f"성별: {answers.get('성별', '미기재')}\n"
                                            f"종족: {answers.get('종족', '미기재')}\n"
                                            f"나이: {answers.get('나이', '미기재')}\n"
                                            f"소속: {answers.get('소속', '미기재')}\n"
                                        )
                                        if answers.get("소속") == "학생":
                                            formatted_description += f"학년 및 반: {answers.get('학년 및 반', '미기재')}\n"
                                        elif answers.get("소속") == "선생님":
                                            formatted_description += f"담당 과목 및 학년, 반: {answers.get('담당 과목 및 학년, 반', '미기재')}\n"
                                        formatted_description += "동아리: 미기재\n\n"
                                        formatted_description += (
                                            f"키/몸무게: {answers.get('키/몸무게', '미기재')}\n"
                                            f"성격: {answers.get('성격', '미기재')}\n"
                                            f"외모: {answers.get('외모', '미기재') if isinstance(answers.get('외모'), str) and not answers.get('외모').startswith('이미지_') else '이미지로 등록됨'}\n\n"
                                            f"체력: {answers.get('체력', '미기재')}\n"
                                            f"지능: {answers.get('지능', '미기재')}\n"
                                            f"이동속도: {answers.get('이동속도', '미기재')}\n"
                                            f"힘: {answers.get('힘', '미기재')}\n"
                                            f"냉철: {answers.get('냉철', '미기재')}\n"
                                        )
                                        techs = []
                                        for i in range(6):
                                            tech_name = answers.get(f"사용 기술/마법/요력_{i}")
                                            if tech_name:
                                                tech_power = answers.get(f"사용 기술/마법/요력 위력_{i}", "미기재")
                                                tech_desc = answers.get(f"사용 기술/마법/요력 설명_{i}", "미기재")
                                                techs.append(f"<{tech_name}> (위력: {tech_power})\n설명: {tech_desc}")
                                        formatted_description += "사용 기술/마법/요력:\n" + "\n\n".join(techs) + "\n" if techs else "사용 기술/마법/요력: 없음\n"
                                        formatted_description += "\n"
                                        formatted_description += (
                                            f"과거사: {answers.get('과거사', '미기재')}\n"
                                            f"특징: {answers.get('특징', '미기재')}\n\n"
                                            f"관계: {answers.get('관계', '미기재')}"
                                        )

                                        char_channel = discord.utils.get(guild.channels, name="캐릭터-목록")
                                        if char_channel:
                                            if thread_id:
                                                thread = bot.get_channel(int(thread_id))
                                                if thread:
                                                    messages = [msg async for msg in thread.history(limit=1, oldest_first=True)]
                                                    if messages:
                                                        await messages[0].edit(content=f"{member.mention}의 캐릭터:\n{formatted_description}", attachments=files if files else [])
                                                    else:
                                                        thread, new_thread_id = await send_message_with_retry(char_channel, f"{member.mention}의 캐릭터:\n{formatted_description}", answers, post_name, files=files)
                                                        thread_id = new_thread_id
                                                else:
                                                    thread, new_thread_id = await send_message_with_retry(char_channel, f"{member.mention}의 캐릭터:\n{formatted_description}", answers, post_name, files=files)
                                                    thread_id = new_thread_id
                                            else:
                                                thread, new_thread_id = await send_message_with_retry(char_channel, f"{member.mention}의 캐릭터:\n{formatted_description}", answers, post_name, files=files)
                                                thread_id = new_thread_id
                                        else:
                                            result += "\n❌ 캐릭터-목록 채널을 못 찾았어! 🥺"
                                else:
                                    failed_fields = []
                                    for field in answers:
                                        if field in reason:
                                            failed_fields.append(field)
                                    result += f"\n다시 입력해야 할 항목: {', '.join(failed_fields) if failed_fields else '알 수 없음'}"

                                await send_message_with_retry(channel, f"{member.mention} {result}")
                                await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("completed", task_id))
                                await db.commit()
                                logger.info(f"Flex 작업 완료: task_id={task_id}, character_id={character_id}")
                                break

                            except Exception as e:
                                await send_message_with_retry(channel, f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")
                                await db.execute("UPDATE flex_tasks SET status = ? WHERE task_id = ?", ("failed", task_id))
                                await db.commit()
                                logger.error(f"Flex 작업 처리 중 에러: task_id={task_id}, 에러: {e}")
                                break
                    break
                except Exception as e:
                    logger.error(f"Flex 작업 조회 중 에러 (시도 {attempt + 1}/{DB_MAX_RETRIES}): {e}")
                    if attempt < DB_MAX_RETRIES - 1:
                        await asyncio.sleep(DB_RETRY_DELAY)
        else:
            await asyncio.sleep(5)  # 큐가 비었을 때 더 긴 대기 시간

# 버튼 뷰 클래스
class SelectionView(discord.ui.View):
    def __init__(self, options, field, user, callback):
        super().__init__(timeout=300.0)
        self.options = options
        self.field = field
        self.user = user
        self.callback = callback
        for option in options:
            button = discord.ui.Button(label=option, style=discord.ButtonStyle.primary)
            button.callback = self.create_button_callback(option)
            self.add_item(button)

    def create_button_callback(self, option):
        async def button_callback(interaction: discord.Interaction):
            if interaction.user != self.user:
                await interaction.response.send_message("이 버튼은 당신이 사용할 수 없어요!", ephemeral=True)
                return
            await interaction.response.defer()
            await self.callback(option, interaction)
            self.stop()
        return button_callback

# 캐릭터 신청 명령어
@bot.tree.command(name="캐릭터_신청", description="캐릭터를 신청해! 순차적으로 질문에 답해줘~")
async def character_apply(interaction: discord.Interaction):
    answers = {}
    tech_counter = 0
    user = interaction.user
    channel = interaction.channel
    logger.info(f"캐릭터 신청 시작: user_id={user.id}")

    can_proceed, error_message = await check_cooldown(str(user.id))
    if not can_proceed:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    # Defer the initial response to prevent timeout
    await interaction.response.defer(ephemeral=True)
    
    # Send initial message using send_message_with_retry with explicit view=None
    await send_message_with_retry(
        channel,
        f"{user.mention} ✅ 캐릭터 신청 시작! 질문에 하나씩 답해줘~ 😊",
        is_interaction=True,
        interaction=interaction,
        view=None,
        ephemeral=True
    )

    async def handle_selection(option, button_interaction):
        answers[question["field"]] = option
        await send_message_with_retry(
            channel,
            f"{user.mention} {question['field']}으로 '{option}' 선택했어!",
            is_interaction=True,
            interaction=button_interaction,
            ephemeral=True
        )
        await asyncio.sleep(0.1)  # 중복 전송 방지

    for question in questions:
        if question.get("condition") and not question["condition"](answers):
            continue
        if question.get("options"):
            view = SelectionView(question["options"], question["field"], user, handle_selection)
            try:
                await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                await view.wait()
                if question["field"] not in answers:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 신청 취소됐어! 다시 시도해~ 🥹")
                    logger.warning(f"캐릭터 신청 타임아웃: user_id={user.id}, field={question['field']}")
                    return
            except discord.HTTPException as e:
                await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                logger.error(f"버튼 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                return
        elif question.get("field") == "사용 기술/마법/요력 추가 여부":
            if tech_counter >= 6:
                continue
            while True:
                try:
                    await send_message_with_retry(channel, f"{user.mention} {question['prompt']}")
                    response = await bot.wait_for(
                        "message",
                        check=lambda m: m.author == user and m.channel == channel,
                        timeout=300.0
                    )
                    answer = response.content.strip()
                    if question["validator"](answer):
                        if answer == "예":
                            for tech_question in questions:
                                if tech_question.get("is_tech"):
                                    while True:
                                        field = f"{tech_question['field']}_{tech_counter}"
                                        await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                                        def check(m):
                                            return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                                        response = await bot.wait_for(
                                            "message",
                                            check=check,
                                            timeout=300.0
                                        )
                                        tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}"
                                        if tech_question["validator"](tech_answer):
                                            answers[field] = tech_answer
                                            break
                                        else:
                                            await send_message_with_retry(channel, tech_question["error_message"])
                            tech_counter += 1
                            if tech_counter < 6:
                                continue
                        break
                    else:
                        await send_message_with_retry(channel, question["error_message"])
                except asyncio.TimeoutError:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 신청 취소됐어! 다시 시도해~ 🥹")
                    logger.warning(f"캐릭터 신청 타임아웃: user_id={user.id}, field={question['field']}")
                    return
                except discord.HTTPException as e:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                    logger.error(f"기술 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                    return
        else:
            while True:
                try:
                    await send_message_with_retry(channel, f"{user.mention} {question['prompt']}")
                    def check(m):
                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                    response = await bot.wait_for(
                        "message",
                        check=check,
                        timeout=300.0
                    )
                    if question["field"] == "외모" and response.attachments:
                        answer = f"이미지_{response.attachments[0].url}"
                    else:
                        answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                    if question["validator"](answer):
                        answers[question["field"]] = answer
                        break
                    else:
                        await send_message_with_retry(channel, question["error_message"])
                except asyncio.TimeoutError:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 신청 취소됐어! 다시 시도해~ 🥹")
                    logger.warning(f"캐릭터 신청 타임아웃: user_id={user.id}, field={question['field']}")
                    return
                except discord.HTTPException as e:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                    logger.error(f"일반 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
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
        await send_message_with_retry(channel, f"{user.mention} {error_msg}다시 입력해줘~")

        for field in fields_to_correct:
            question = next(q for q in questions if q["field"] == field)
            while True:
                if question.get("options"):
                    view = SelectionView(question["options"], question["field"], user, handle_selection)
                    try:
                        await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                        await view.wait()
                        if question["field"] not in answers:
                            await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                            logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                            return
                        break
                    except discord.HTTPException as e:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                        logger.error(f"수정 버튼 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                        return
                else:
                    try:
                        await send_message_with_retry(channel, f"{user.mention} {field}을 다시 입력해: {question['prompt']}")
                        def check(m):
                            return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                        response = await bot.wait_for(
                            "message",
                            check=check,
                            timeout=300.0
                        )
                        if field == "외모" and response.attachments:
                            answer = f"이미지_{response.attachments[0].url}"
                        else:
                            answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                        if question["validator"](answer):
                            answers[field] = answer
                            break
                        else:
                            await send_message_with_retry(channel, question["error_message"])
                    except asyncio.TimeoutError:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                        logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                        return
                    except discord.HTTPException as e:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                        logger.error(f"수정 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                        return

    # AI 심사에서 외모 필드 제외
    description = "\n".join([f"{field}: {answers[field]}" for field in answers if field != "외모"])
    allowed_roles, _ = await get_settings(interaction.guild.id)
    prompt = DEFAULT_PROMPT.format(
        banned_words=', '.join(BANNED_WORDS),
        required_fields=', '.join(REQUIRED_FIELDS),
        allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
        allowed_roles=', '.join(allowed_roles),
        description=description
    )
    character_id = str(uuid.uuid4())
    task_id = await queue_flex_task(character_id, description, str(user.id), str(channel.id), None, "character_check", prompt)
    if task_id:
        await save_result(character_id, description, False, "심사 중", None, str(user.id), answers.get("이름"), answers.get("종족"), answers.get("나이"), answers.get("성별"), None, answers.get("포스트 이름"))
        await send_message_with_retry(
            channel,
            f"{user.mention} ⏳ 심사 중이야! 곧 결과 알려줄게~ 😊",
            is_interaction=True,
            interaction=interaction,
            ephemeral=True
        )
        logger.info(f"캐릭터 심사 요청 완료: character_id={character_id}, user_id={user.id}")
    else:
        await send_message_with_retry(
            channel,
            f"{user.mention} ❌ 심사 요청에 실패했어! 다시 시도해~ 🥹",
            is_interaction=True,
            interaction=interaction,
            ephemeral=True
        )
        logger.error(f"캐릭터 심사 요청 실패: character_id={character_id}, user_id={user.id}")

# 캐릭터 수정 명령어
@bot.tree.command(name="캐릭터_수정", description="등록된 캐릭터를 수정해! 포스트 이름을 입력해줘~")
async def character_edit(interaction: discord.Interaction, post_name: str):
    answers = {}
    tech_counter = 0
    user = interaction.user
    channel = interaction.channel
    logger.info(f"캐릭터 수정 시작: user_id={user.id}, post_name={post_name}")

    can_proceed, error_message = await check_cooldown(str(user.id))
    if not can_proceed:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    characters = await find_characters_by_post_name(post_name, str(user.id))
    if not characters:
        await interaction.response.send_message(f"{user.mention} ❌ '{post_name}'에 해당하는 포스트가 없어! /캐릭터_신청으로 등록해줘~ 🥺", ephemeral=True)
        logger.warning(f"캐릭터 수정 실패: 포스트 없음, post_name={post_name}, user_id={user.id}")
        return

    selected_char = characters[0]
    character_id, _, _, _, _, thread_id, _ = selected_char
    answers = await get_character_info(character_id)
    if not answers:
        await interaction.response.send_message(f"{user.mention} ❌ 캐릭터 정보를 불러올 수 없어! 다시 시도해~ 🥹", ephemeral=True)
        logger.error(f"캐릭터 정보 로드 실패: character_id={character_id}, user_id={user.id}")
        return

    answers["포스트 이름"] = post_name
    await interaction.response.defer(ephemeral=True)
    await send_message_with_retry(
        channel,
        f"{user.mention} ✅ '{post_name}' 수정 시작! 수정할 항목 번호를 쉼표로 구분해 입력해줘~",
        is_interaction=True,
        interaction=interaction,
        ephemeral=True
    )
    fields_list = "\n".join([f"{i+1}. {field}" for i, field in enumerate(EDITABLE_FIELDS)])
    await send_message_with_retry(channel, f"{user.mention} 수정할 항목 번호를 쉼표로 구분해 입력해줘 (예: 1,3,5). 기술/마법/요력 수정은 {EDITABLE_FIELDS.index('사용 기술/마법/요력') + 1}번 선택!\n{fields_list}")

    try:
        response = await bot.wait_for(
            "message",
            check=lambda m: m.author == user and m.channel == channel,
            timeout=300.0
        )
        selected_indices = [int(i.strip()) - 1 for i in response.content.split(",")]
        if not all(0 <= i < len(EDITABLE_FIELDS) for i in selected_indices):
            await send_message_with_retry(channel, f"{user.mention} ❌ 유효한 번호를 입력해줘! 다시 시도해~ 🥹")
            logger.warning(f"캐릭터 수정 실패: 유효하지 않은 번호, user_id={user.id}")
            return
    except (ValueError, asyncio.TimeoutError):
        await send_message_with_retry(channel, f"{user.mention} ❌ 잘못된 입력이거나 시간이 초과됐어! 다시 시도해~ 🥹")
        logger.warning(f"캐릭터 수정 실패: 입력 오류 또는 타임아웃, user_id={user.id}")
        return

    async def handle_selection(option, button_interaction):
        answers[question["field"]] = option
        await send_message_with_retry(
            channel,
            f"{user.mention} {question['field']}으로 '{option}' 선택했어!",
            is_interaction=True,
            interaction=button_interaction,
            ephemeral=True
        )
        await asyncio.sleep(0.1)  # 중복 전송 방지

    # 일반 항목 수정
    for index in selected_indices:
        if "사용 기술/마법/요력" in EDITABLE_FIELDS[index]:
            continue
        question = next(q for q in questions if q["field"] == EDITABLE_FIELDS[index])
        while True:
            if question.get("options"):
                view = SelectionView(question["options"], question["field"], user, handle_selection)
                try:
                    await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                    await view.wait()
                    if question["field"] not in answers:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                        logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                        return
                    break
                except discord.HTTPException as e:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                    logger.error(f"수정 버튼 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                    return
            else:
                try:
                    await send_message_with_retry(channel, f"{user.mention} {question['field']}을 수정해: {question['prompt']}")
                    def check(m):
                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                    response = await bot.wait_for(
                        "message",
                        check=check,
                        timeout=300.0
                    )
                    if question["field"] == "외모" and response.attachments:
                        answer = f"이미지_{response.attachments[0].url}"
                    else:
                        answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                    if question["validator"](answer):
                        answers[question["field"]] = answer
                        break
                    else:
                        await send_message_with_retry(channel, question["error_message"])
                except asyncio.TimeoutError:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                    logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                    return
                except discord.HTTPException as e:
                    await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                    logger.error(f"수정 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                    return

    # 기술/마법/요력 수정
    if any("사용 기술/마법/요력" in EDITABLE_FIELDS[i] for i in selected_indices):
        techs = [(k, answers[k], answers.get(f"사용 기술/마법/요력 위력_{k.split('_')[1]}"), answers.get(f"사용 기술/마법/요력 설명_{k.split('_')[1]}"))
                 for k in sorted([k for k in answers if k.startswith("사용 기술/마법/요력_")], key=lambda x: int(x.split('_')[1]))]
        tech_counter = len(techs)
        tech_list = "\n".join([f"{i+1}. {t[1]} (위력: {t[2]}, 설명: {t[3]})" for i, t in enumerate(techs)]) if techs else "없음"
        await send_message_with_retry(channel, f"{user.mention} 현재 기술/마법/요력:\n{tech_list}\n수정하려면 번호, 추가하려면 'a', 삭제하려면 'd'로 입력 (예: 1,a,d)")
        try:
            response = await bot.wait_for(
                "message",
                check=lambda m: m.author == user and m.channel == channel,
                timeout=300.0
            )
            actions = [a.strip() for a in response.content.split(",")]
        except asyncio.TimeoutError:
            await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
            logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field=기술/마법/요력")
            return

        for action in actions:
            if action.isdigit():
                idx = int(action) - 1
                if 0 <= idx < len(techs):
                    for tech_question in questions:
                        if tech_question.get("is_tech"):
                            while True:
                                field = f"{tech_question['field']}_{techs[idx][0].split('_')[1]}"
                                await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                                def check(m):
                                    return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                                try:
                                    response = await bot.wait_for(
                                        "message",
                                        check=check,
                                        timeout=300.0
                                    )
                                    tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                                    if tech_question["validator"](tech_answer):
                                        answers[field] = tech_answer
                                        break
                                    else:
                                        await send_message_with_retry(channel, tech_question["error_message"])
                                except asyncio.TimeoutError:
                                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                                    logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={tech_question['field']}")
                                    return
                                except discord.HTTPException as e:
                                    await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                                    logger.error(f"기술 수정 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                                    return
            elif action == "a" and len(techs) < 6:
                for tech_question in questions:
                    if tech_question.get("is_tech"):
                        while True:
                            field = f"{tech_question['field']}_{tech_counter}"
                            await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                            def check(m):
                                return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                            try:
                                response = await bot.wait_for(
                                    "message",
                                    check=check,
                                    timeout=300.0
                                )
                                tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                                if tech_question["validator"](tech_answer):
                                    answers[field] = tech_answer
                                    break
                                else:
                                    await send_message_with_retry(channel, tech_question["error_message"])
                            except asyncio.TimeoutError:
                                await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                                logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={tech_question['field']}")
                                return
                            except discord.HTTPException as e:
                                await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                                logger.error(f"기술 추가 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                                return
                tech_counter += 1
            elif action == "d" and techs:
                await send_message_with_retry(channel, f"{user.mention} 삭제할 기술 번호를 입력해줘 (1-{len(techs)})")
                try:
                    response = await bot.wait_for(
                        "message",
                        check=lambda m: m.author == user and m.channel == channel,
                        timeout=300.0
                    )
                    idx = int(response.content.strip()) - 1
                    if 0 <= idx < len(techs):
                        key = techs[idx][0]
                        del answers[key]
                        del answers[f"사용 기술/마법/요력 위력_{key.split('_')[1]}"]
                        del answers[f"사용 기술/마법/요력 설명_{key.split('_')[1]}"]
                        tech_counter -= 1
                    else:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 유효한 번호를 입력해줘! 다시 시도해~ 🥹")
                        logger.warning(f"캐릭터 수정 실패: 유효하지 않은 기술 번호, user_id={user.id}")
                except (ValueError, asyncio.TimeoutError):
                    await send_message_with_retry(channel, f"{user.mention} ❌ 잘못된 입력이거나 시간이 초과됐어! 다시 시도해~ 🥹")
                    logger.warning(f"캐릭터 수정 실패: 기술 삭제 입력 오류 또는 타임아웃, user_id={user.id}")
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
        await send_message_with_retry(channel, f"{user.mention} {error_msg}다시 입력해줘~")

        for field in fields_to_correct:
            question = next(q for q in questions if q["field"] == field)
            while True:
                if question.get("options"):
                    view = SelectionView(question["options"], question["field"], user, handle_selection)
                    try:
                        await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                        await view.wait()
                        if question["field"] not in answers:
                            await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                            logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                            return
                        break
                    except discord.HTTPException as e:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                        logger.error(f"수정 버튼 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                        return
                else:
                    try:
                        await send_message_with_retry(channel, f"{user.mention} {field}을 다시 입력해: {question['prompt']}")
                        def check(m):
                            return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                        response = await bot.wait_for(
                            "message",
                            check=check,
                            timeout=300.0
                        )
                        if field == "외모" and response.attachments:
                            answer = f"이미지_{response.attachments[0].url}"
                        else:
                            answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                        if question["validator"](answer):
                            answers[field] = answer
                            break
                        else:
                            await send_message_with_retry(channel, question["error_message"])
                    except asyncio.TimeoutError:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                        logger.warning(f"캐릭터 수정 타임아웃: user_id={user.id}, field={question['field']}")
                        return
                    except discord.HTTPException as e:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 통신 오류야! {str(e)} 다시 시도해~ 🥹")
                        logger.error(f"수정 입력 메시지 전송 중 에러: user_id={user.id}, 에러: {e}")
                        return

    # AI 심사에서 외모 필드 제외
    description = "\n".join([f"{field}: {answers[field]}" for field in answers if field != "외모"])
    allowed_roles, _ = await get_settings(interaction.guild.id)
    prompt = DEFAULT_PROMPT.format(
        banned_words=', '.join(BANNED_WORDS),
        required_fields=', '.join(REQUIRED_FIELDS),
        allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
        allowed_roles=', '.join(allowed_roles),
        description=description
    )
    task_id = await queue_flex_task(character_id, description, str(user.id), str(channel.id), thread_id, "character_check", prompt)
    if task_id:
        await send_message_with_retry(
            channel,
            f"{user.mention} ⏳ 수정 심사 중이야! 곧 결과 알려줄게~ 😊",
            is_interaction=True,
            interaction=interaction,
            ephemeral=True
        )
        logger.info(f"캐릭터 수정 심사 요청 완료: character_id={character_id}, user_id={user.id}")
    else:
        await send_message_with_retry(
            channel,
            f"{user.mention} ❌ 심사 요청에 실패했어! 다시 시도해~ 🥹",
            is_interaction=True,
            interaction=interaction,
            ephemeral=True
        )
        logger.error(f"캐릭터 수정 심사 요청 실패: character_id={character_id}, user_id={user.id}")

# 캐릭터 목록 명령어
@bot.tree.command(name="캐릭터_목록", description="등록된 캐릭터 목록을 확인해!")
async def character_list(interaction: discord.Interaction):
    user = interaction.user
    logger.info(f"캐릭터 목록 요청: user_id={user.id}")
    characters = await find_characters_by_post_name("", str(user.id))  # 빈 post_name으로 전체 조회
    if not characters:
        await interaction.response.send_message("등록된 캐릭터가 없어! /캐릭터_신청으로 등록해줘~ 🥺", ephemeral=True)
        logger.info(f"캐릭터 목록 없음: user_id={user.id}")
        return
    char_list = "\n".join([f"- {c[1]} (포스트: {c[6]})" for c in characters])
    await interaction.response.send_message(f"**너의 캐릭터 목록**:\n{char_list}", ephemeral=True)
    logger.info(f"캐릭터 목록 반환: user_id={user.id}, 캐릭터 수={len(characters)}")

# 봇 시작 시 실행
@bot.event
async def on_ready():
    try:
        await init_db()
        logger.info(f'봇이 로그인했어: {bot.user}')
        await bot.tree.sync()
        bot.loop.create_task(process_flex_queue())
        logger.info("봇 초기화 완료, Flex 큐 처리 시작")
    except Exception as e:
        logger.error(f"봇 시작 중 에러 발생: {e}")
        raise e

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    # Flask 서버를 별도 스레드에서 실행
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))),
        daemon=True
    )
    flask_thread.start()
    logger.info("Flask 서버 시작")

    # 디스코드 봇 실행
    bot.run(DISCORD_TOKEN)
