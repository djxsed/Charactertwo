import discord
from discord.ext import commands
import os
import json
import aiosqlite
import re
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import hashlib
import uuid
import asyncio
import time
from flask import Flask
import threading
import logging

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
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
try:
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 .env 파일에 없어!")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI 클라이언트 초기화 성공")
except Exception as e:
    logger.error(f"OpenAI 클라이언트 초기화 실패: {str(e)}")
    raise

# 봇 설정
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# 상수 정의
BANNED_WORDS = ["악마", "천사", "이세계", "드래곤"]
REQUIRED_FIELDS = ["이름", "나이", "성격"]
LOG_CHANNEL_ID = 1358060156742533231
COOLDOWN_SECONDS = 5
MAX_REQUESTS_PER_DAY = 1000
CHARACTER_LIST_CHANNEL = "캐릭터-목록"
ALLOWED_RACES = ["인간", "마법사", "AML", "요괴"]
ALLOWED_ROLES = ["학생", "선생님", "AML"]
CHECK_CHANNEL_NAME = "입학-신청서"
MAX_SKILLS = 6
TIMEOUT_SECONDS = 300  # 답변 대기 시간 (5분)

# 정규 표현식
AGE_PATTERN = r"^\d+$"
GRADE_CLASS_PATTERN = r"(\d)[-\s/](\d)반|(\d)학년\s*(\d)반"
SUBJECT_PATTERN = r"(.+),\s*(\d)[-\s/](\d)반|(.+),\s*(\d)학년\s*(\d)반"

# 데이터베이스 초기화
async def init_db():
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                character_id TEXT PRIMARY KEY,
                user_id TEXT,
                guild_id TEXT,
                description TEXT,
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
                result TEXT,
                created_at TEXT
            )
        """)
        await db.commit()

# 서버별 설정 조회
async def get_settings(guild_id):
    try:
        async with aiosqlite.connect("characters.db") as db:
            async with db.execute("SELECT allowed_roles, check_channel_name FROM settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    allowed_roles = row[0].split(",") if row[0] else ALLOWED_ROLES
                    check_channel_name = row[1] if row[1] else CHECK_CHANNEL_NAME
                    return allowed_roles, check_channel_name
                return ALLOWED_ROLES, CHECK_CHANNEL_NAME
    except Exception as e:
        logger.error(f"설정 조회 실패: guild_id={guild_id}, error={str(e)}")
        return ALLOWED_ROLES, CHECK_CHANNEL_NAME

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

# 캐릭터 정보 저장
async def save_character(character_id, user_id, guild_id, description, role_name):
    timestamp = datetime.utcnow().isoformat()
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO characters (character_id, user_id, guild_id, description, role_name, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (character_id, user_id, guild_id, description, role_name, timestamp))
        await db.commit()

# 캐릭터 심사 결과 저장
async def save_character_result(character_id: str, description: str, pass_status: bool, reason: str, role_name: str):
    try:
        description_hash = hashlib.md5(description.encode()).hexdigest()
        timestamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect("characters.db") as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO results (character_id, description_hash, pass, reason, role_name, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (character_id, description_hash, pass_status, reason, role_name, timestamp)
            )
            await db.commit()
            logger.info(f"캐릭터 심사 결과 저장: character_id={character_id}, pass={pass_status}")
    except Exception as e:
        logger.error(f"캐릭터 결과 저장 실패: character_id={character_id}, error={str(e)}")

# 답변 검증 함수
async def validate_answer(field, value, character_data):
    if not value.strip():
        return False, "❌ 값이 비어있어! 다시 입력해~ 😊"

    if field == "종족":
        if value not in ALLOWED_RACES:
            return False, f"❌ 종족은 {', '.join(ALLOWED_RACES)} 중 하나야! 다시 골라~ 😄"
        if value == "AML" and character_data.get("소속") == "학생":
            return False, "❌ AML은 학생이 될 수 없어! 다시 확인해~ 🤔"

    elif field == "이름":
        if any(word in value for word in BANNED_WORDS):
            return False, f"❌ 이름에 금지 단어({', '.join(BANNED_WORDS)}) 포함! 다른 이름 써~ 😅"
        if len(value) > 50:
            return False, "❌ 이름 너무 길어! 50자 이내로~ 📝"

    elif field == "성별":
        if value not in ["남성", "여성", "기타"]:
            return False, "❌ 성별은 '남성', '여성', '기타' 중 하나야! 다시 입력해~ 😊"

    elif field == "나이":
        if not re.match(AGE_PATTERN, value):
            return False, "❌ 나이는 숫자만 입력! 예: 30 😄"
        age = int(value)
        if not (1 <= age <= 5000):
            return False, f"❌ 나이 {age}살? 1~5000살로~ 🕰️"

    elif field == "키/몸무게":
        if not re.match(r"\d+/\d+", value):
            return False, "❌ 키/몸무게는 '키/몸무게' 형식! 예: 170/60 😅"

    elif field == "성격":
        if len(value) < 10:
            return False, "❌ 성격은 10자 이상 자세히! 어떤 캐릭터야? 😊"

    elif field == "외모 글묘사":
        if len(value) < 20:
            return False, "❌ 외모는 20자 이상 자세히 묘사해! 생김새가 궁금해~ 😄"

    elif field == "소속":
        if value not in ALLOWED_ROLES:
            return False, f"❌ 소속은 {', '.join(ALLOWED_ROLES)} 중 하나야! 다시 골라~ 😊"
        if value == "AML" and character_data.get("종족") == "요괴":
            return False, "❌ AML 소속은 요괴가 될 수 없어(정체 숨김 제외)! 다시 확인해~ 🤔"

    elif field == "학년, 반":
        if not re.match(GRADE_CLASS_PATTERN, value):
            return False, "❌ 학년, 반은 'x-y반' 또는 'x학년 y반' 형식! 예: 3-1반 😅"

    elif field == "담당 과목 및 학년, 반":
        if not re.match(SUBJECT_PATTERN, value):
            return False, "❌ 담당 과목 및 학년, 반은 '과목, x-y반' 또는 '과목, x학년 y반' 형식! 예: 수학, 3-1반 😅"

    elif field in ["체력", "지능", "이동속도", "힘"]:
        try:
            num = int(value)
            if not (1 <= num <= 6):
                return False, f"❌ {field}은 1~6 사이! 다시 입력해~ 💪"
        except ValueError:
            return False, f"❌ {field}은 숫자야! 예: 3 😄"

    elif field == "냉철":
        try:
            num = int(value)
            if not (1 <= num <= 4):
                return False, f"❌ 냉철은 1~4 사이! 다시 입력해~ 🧠"
        except ValueError:
            return False, "❌ 냉철은 숫자야! 예: 2 😄"

    elif field == "사용 기술/마법/요력":
        if len(value) > 50:
            return False, "❌ 기술 이름은 50자 이내로! 간결하게~ 📝"
        if any(word in value for word in BANNED_WORDS):
            return False, f"❌ 기술 이름에 금지 단어({', '.join(BANNED_WORDS)}) 포함! 다른 이름 써~ 😅"

    elif field == "사용 기술/마법/요력의 위력":
        try:
            num = int(value)
            if not (1 <= num <= 5):
                return False, f"❌ 위력은 1~5 사이! 다시 입력해~ 🔥"
        except ValueError:
            return False, "❌ 위력은 숫자야! 예: 3 😄"

    elif field == "사용 기술/마법/요력 설명":
        if len(value) < 20:
            return False, "❌ 기술 설명은 20자 이상 자세히! 어떻게 작동해? 😊"
        if any(word in value for word in ["시간", "현실", "정신"]):
            return False, "❌ 시간/현실 조작, 정신 계열 능력은 금지야! 다른 설명 써~ 😅"

    elif field == "사용 기술/마법/요력 추가 여부":
        if value not in ["예", "아니오"]:
            return False, "❌ '예' 또는 '아니오'로 답해! 기술 더 추가할까? 😊"

    elif field == "과거사":
        if len(value) < 30:
            return False, "❌ 과거사는 30자 이상 자세히! 어떤 삶을 살아왔어? 😊"
        if any(word in value for word in ["시간 여행", "초자연", "비현실"]):
            return False, "❌ 시간 여행, 초자연적, 비현실적 과거는 금지야! 현실적으로 써~ 😅"

    elif field == "특징":
        if len(value) < 10:
            return False, "❌ 특징은 10자 이상! 뭐가 특별해? 😄"

    elif field == "관계":
        if value.lower() == "없음":
            return True, ""
        if len(value) < 10:
            return False, "❌ 관계는 10자 이상 자세히! 누구와 어떤 관계야? 😊"

    return True, ""

# Batch 작업 관련 함수
async def queue_batch_task(character_id, description, user_id, channel_id, thread_id, task_type, prompt):
    task_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    async with aiosqlite.connect("characters.db") as db:
        await db.execute("""
            INSERT INTO flex_tasks (task_id, character_id, description, user_id, channel_id, thread_id, type, prompt, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?,66 ?, ?, ?)
        """, (task_id, character_id, description, user_id, channel_id, thread_id, task_type, prompt, "pending", created_at))
        await db.commit()
    logger.info(f"Batch 작업 큐에 추가: task_id={task_id}, type={task_type}")
    return task_id

async def update_task_status(task_id: str, status: str, result: dict = None):
    try:
        async with aiosqlite.connect("characters.db") as db:
            if result:
                await db.execute(
                    "UPDATE flex_tasks SET status = ?, result = ? WHERE task_id = ?",
                    (status, json.dumps(result), task_id)
                )
            else:
                await db.execute(
                    "UPDATE flex_tasks SET status = ? WHERE task_id = ?",
                    (status, task_id)
                )
            await db.commit()
            logger.info(f"작업 상태 업데이트: task_id={task_id}, status={status}")
    except Exception as e:
        logger.error(f"작업 상태 업데이트 실패: task_id={task_id}, error={str(e)}")

async def get_pending_tasks():
    try:
        async with aiosqlite.connect("characters.db") as db:
            async with db.execute("""
                SELECT task_id, character_id, description, user_id, channel_id, thread_id, type, prompt
                FROM flex_tasks WHERE status = 'pending' LIMIT 50
            """) as cursor:
                tasks = await cursor.fetchall()
                logger.info(f"가져온 대기 중인 작업 수: {len(tasks)}")
                return tasks
    except Exception as e:
        logger.error(f"작업 가져오기 실패: {str(e)}")
        return []

def create_jsonl_file(tasks: list, filename: str):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            for task in tasks:
                task_id, _, _, _, _, _, task_type, prompt = task
                request = {
                    "custom_id": task_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": "gpt-4.1-nano",
                        "messages": [
                            {"role": "system", "content": "You are a Discord bot for character review."},
                            {"role": "user", "content": prompt}
                        ],
                        "max_tokens": 150
                    }
                }
                f.write(json.dumps(request) + "\n")
        logger.info(f".jsonl 파일 생성: {filename}")
    except Exception as e:
        logger.error(f".jsonl 파일 생성 실패: {str(e)}")
        raise

async def process_batch():
    logger.info("Batch 처리 시작")
    while True:
        try:
            tasks = await get_pending_tasks()
            if not tasks:
                logger.info("대기 중인 작업이 없습니다. 30초 대기...")
                await asyncio.sleep(30)
                continue

            jsonl_filename = f"batch_{int(time.time())}.jsonl"
            create_jsonl_file(tasks, jsonl_filename)

            try:
                with open(jsonl_filename, "rb") as f:
                    file_response = openai_client.files.create(file=f, purpose="batch")
                file_id = file_response.id
                logger.info(f"파일 업로드 성공: file_id={file_id}")

                batch_response = openai_client.batches.create(
                    input_file_id=file_id,
                    endpoint="/v1/chat/completions",
                    completion_window="24h",
                    metadata={"description": "Character review batch"}
                )
                batch_id = batch_response.id
                logger.info(f"Batch 작업 생성: batch_id={batch_id}")

                for task in tasks:
                    task_id = task[0]
                    await update_task_status(task_id, "processing")

                while True:
                    batch_status = openai_client.batches.retrieve(batch_id)
                    logger.info(f"Batch 상태: batch_id={batch_id}, status={batch_status.status}")
                    if batch_status.status in ["completed", "failed"]:
                        break
                    await asyncio.sleep(15)

                if batch_status.status == "failed":
                    logger.error(f"Batch 실패: batch_id={batch_id}, errors={batch_status.errors}")
                    for task in tasks:
                        task_id, _, _, user_id, channel_id, thread_id, task_type, _ = task
                        await update_task_status(task_id, "failed", {"error": "Batch 작업 실패"})
                        await send_discord_message(
                            channel_id, thread_id, user_id,
                            f"❌ 앗, Batch 처리 중 오류가 났어... 다시 시도해줄래? 🥺"
                        )
                    continue

                output_file_id = batch_status.output_file_id
                output_content = openai_client.files.content(output_file_id).text
                results = [json.loads(line) for line in output_content.splitlines()]
                logger.info(f"Batch 결과 가져옴: {len(results)}개 작업")

                for result in results:
                    task_id = result["custom_id"]
                    task = next((t for t in tasks if t[0] == task_id), None)
                    if not task:
                        logger.warning(f"작업을 찾을 수 없음: task_id={task_id}")
                        continue

                    _, character_id, description, user_id, channel_id, thread_id, task_type, _ = task

                    if "error" in result:
                        error_message = result["error"]["message"]
                        logger.error(f"작업 오류: task_id={task_id}, error={error_message}")
                        await update_task_status(task_id, "failed", {"error": error_message})
                        if task_type == "character_check":
                            await save_character_result(character_id, description, False, f"오류: {error_message}", None)
                            await send_discord_message(
                                channel_id, thread_id, user_id,
                                f"❌ 앗, 심사 중 오류가 났어: {error_message} 😓"
                            )
                        continue

                    response = result["response"]["body"]["choices"][0]["message"]["content"]
                    await update_task_status(task_id, "completed", {"response": response})

                    if task_type == "character_check":
                        pass_status = "✅" in response
                        role_name = None
                        reason = response.replace("✅", "").replace("❌", "").strip()
                        guild_id = int(channel_id.split("-")[0]) if "-" in channel_id else int(channel_id)
                        allowed_roles, _ = await get_settings(guild_id)

                        if pass_status:
                            for role in allowed_roles:
                                if f"역할: {role}" in response:
                                    role_name = role
                                    break
                            if not role_name or role_name not in allowed_roles:
                                await save_character_result(character_id, description, False, f"유효한 역할 없음 (허용된 역할: {', '.join(allowed_roles)})", None)
                                message = f"❌ 앗, 유효한 역할이 없네! {', '.join(allowed_roles)} 중 하나로 설정해줘~ 😊"
                            else:
                                await save_character_result(character_id, description, True, "통과", role_name)
                                message = f"🎉 우와, 대단해! 통과했어~ 역할: {role_name} 🎊"
                        else:
                            await save_character_result(character_id, description, False, reason, None)
                            message = f"❌ 아쉽게도... {reason} 다시 수정해서 도전해봐! 내가 응원할게~ 💪"

                        if pass_status and role_name:
                            try:
                                guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
                                if guild:
                                    member = await guild.fetch_member(int(user_id))
                                    has_role = False
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
                                        message = "🎉 이미 통과된 캐릭터야~ 역할은 이미 있어! 🎊"
                                    else:
                                        if role:
                                            try:
                                                await member.add_roles(role)
                                                message += f" (역할 `{role_name}` 부여했어! 😊)"
                                            except discord.Forbidden:
                                                message += f" (역할 `{role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                                        else:
                                            message += f" (역할 `{role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

                                        if race_role:
                                            try:
                                                await member.add_roles(race_role)
                                                message += f" (종족 역할 `{race_role_name}` 부여했어! 😊)"
                                            except discord.Forbidden:
                                                message += f" (종족 역할 `{race_role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                                        elif race_role_name:
                                            message += f" (종족 역할 `{race_role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"
                                else:
                                    message += " (서버를 찾을 수 없어... 🥺)"
                            except Exception as e:
                                message += f" (역할 부여 실패: {str(e)} 🥺)"
                                logger.error(f"역할 부여 실패: user_id={user_id}, role={role_name}, error={str(e)}")

                        await send_discord_message(channel_id, thread_id, user_id, message)

                log_channel = bot.get_channel(LOG_CHANNEL_ID)
                if log_channel:
                    try:
                        await log_channel.send(f"Batch {batch_id} 완료: {len(results)}개 작업 처리")
                        logger.info(f"Batch 완료 로그 전송: batch_id={batch_id}")
                    except Exception as e:
                        logger.error(f"Batch 완료 로그 전송 실패: {str(e)}")

            except Exception as e:
                logger.error(f"Batch 처리 중 오류: {str(e)}")
                for task in tasks:
                    task_id, _, _, user_id, channel_id, thread_id, task_type, _ = task
                    await update_task_status(task_id, "failed", {"error": str(e)})
                    await send_discord_message(
                        channel_id, thread_id, user_id,
                        f"❌ 앗, 처리 중 오류가 났어: {str(e)} 다시 시도해줄래? 🥺"
                    )
                log_channel = bot.get_channel(LOG_CHANNEL_ID)
                if log_channel:
                    try:
                        await log_channel.send(f"Batch 처리 오류: {str(e)}")
                    except Exception as log_error:
                        logger.error(f"Batch 오류 로그 전송 실패: {str(log_error)}")

            finally:
                if os.path.exists(jsonl_filename):
                    try:
                        os.remove(jsonl_filename)
                        logger.info(f".jsonl 파일 삭제: {jsonl_filename}")
                    except Exception as e:
                        logger.error(f".jsonl 파일 삭제 실패: {str(e)}")

        except Exception as e:
            logger.error(f"Batch 처리 루프 오류: {str(e)}")
            await asyncio.sleep(60)

async def send_discord_message(channel_id: str, thread_id: str, user_id: str, message: str):
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        if not channel:
            raise ValueError(f"채널을 찾을 수 없음: channel_id={channel_id}")

        if thread_id:
            thread = channel.get_thread(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if not thread:
                raise ValueError(f"스레드를 찾을 수 없음: thread_id={thread_id}")
            await thread.send(f"<@{user_id}> {message}")
            logger.info(f"스레드에 메시지 전송: thread_id={thread_id}, user_id={user_id}")
        else:
            await channel.send(f"<@{user_id}> {message}")
            logger.info(f"채널에 메시지 전송: channel_id={channel_id}, user_id={user_id}")
    except Exception as e:
        logger.error(f"디스코드 메시지 전송 실패: channel_id={channel_id}, thread_id={thread_id}, error={str(e)}")
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(f"디스코드 메시지 전송 오류: {str(e)}")
            except Exception as log_error:
                logger.error(f"로그 채널 메시지 전송 실패: {str(log_error)}")

# OpenAI 프롬프트 생성
async def create_openai_prompt(character_data, guild_id):
    allowed_roles, _ = await get_settings(guild_id)
    description = "\n".join([f"{k}: {v}" for k, v in character_data.items() if k != "사용 기술/마법/요력"] + [
        f"사용 기술/마법/요력: {skill['name']} (위력: {skill['power']}) {skill['description']}" for skill in character_data.get("사용 기술/마법/요력", [])
    ])
    prompt = f"""
    디스코드 역할극 서버의 캐릭터 심사 봇이야. 캐릭터 설명을 보고:
    1. 서버 규칙에 맞는지 판단해.
    2. 캐릭터가 {allowed_roles} 중 하나인지 정해.
    **간결하게 50자 이내로 답변해.**

    **규칙**:
    - 금지 단어: {', '.join(BANNED_WORDS)}.
    - 필수 항목: {', '.join(REQUIRED_FIELDS)}.
    - 허용 종족: {', '.join(ALLOWED_RACES)}.
    - 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법 위력(1~5).
    - 소속: A.M.L, 하람고, 하람고등학교만 허용.
    - 속성 합산: 인간 5~16, 마법사 5~17, 요괴 5~18.
    - 학년 및 반: 'x-y반', 'x학년 y반' 형식.
    - 기술/마법/요력: 시간, 범위, 위력 명확, 과도 금지.
    - 기술/마법/요력: 최대 6개.
    - AML 소속 시 요괴 불가(정체 숨김 맥락 제외).
    - 위력 4~5는 쿨타임/리스크 필수.
    - 치유/방어 계열 역계산.
    - 정신 계열 능력 불가.

    **캐릭터 설명**:
    {description}

    **응답 형식**:
    - 통과: "✅ 역할: [역할]"
    - 실패: "❌ [실패 이유]"
    """
    return prompt

# 캐릭터 신청 프로세스
async def character_application(interaction: discord.Interaction):
    user = interaction.user
    guild = interaction.guild
    character_id = str(uuid.uuid4())
    character_data = {}
    skills = []
    current_skill = {}

    questions = [
        {"field": "종족", "prompt": f"캐릭터의 종족은? ({', '.join(ALLOWED_RACES)}) 😊"},
        {"field": "이름", "prompt": "캐릭터 이름은? (금지 단어: {', '.join(BANNED_WORDS)}) 😄"},
        {"field": "성별", "prompt": "캐릭터의 성별은? (남성, 여성, 기타) 😊"},
        {"field": "나이", "prompt": "캐릭터의 나이는? (1~5000살, 숫자만) 🕰️"},
        {"field": "키/몸무게", "prompt": "캐릭터의 키/몸무게는? (예: 170/60) 📏"},
        {"field": "성격", "prompt": "캐릭터의 성격은? (10자 이상 자세히) 😄"},
        {"field": "외모 글묘사", "prompt": "캐릭터의 외모를 묘사해! (20자 이상) 😊"},
        {"field": "소속", "prompt": f"캐릭터의 소속은? ({', '.join(ALLOWED_ROLES)}) 🏫"},
        {"field": "학년, 반", "prompt": "학년과 반은? (예: 3-1반) 😊", "condition": lambda data: data.get("소속") == "학생"},
        {"field": "담당 과목 및 학년, 반", "prompt": "담당 과목 및 학년, 반은? (예: 수학, 3-1반) 😊", "condition": lambda data: data.get("소속") == "선생님"},
        {"field": "체력", "prompt": "캐릭터의 체력은? (1~6) 💪"},
        {"field": "지능", "prompt": "캐릭터의 지능은? (1~6) 🧠"},
        {"field": "이동속도", "prompt": "캐릭터의 이동속도는? (1~6) 🏃"},
        {"field": "힘", "prompt": "캐릭터의 힘은? (1~6) 💪"},
        {"field": "냉철", "prompt": "캐릭터의 냉철은? (1~4) 😎"},
        {"field": "사용 기술/마법/요력", "prompt": "사용 기술/마법/요력 이름은? (50자 이내) 🔥"},
        {"field": "사용 기술/마법/요력의 위력", "prompt": "기술의 위력은? (1~5) 💥"},
        {"field": "사용 기술/마법/요력 설명", "prompt": "기술을 자세히 설명해! (20자 이상) 📜"},
        {"field": "사용 기술/마법/요력 추가 여부", "prompt": f"기술을 더 추가할까? (예/아니오, 현재 {len(skills)}/{MAX_SKILLS}) 😊"},
        {"field": "과거사", "prompt": "캐릭터의 과거사는? (30자 이상, 현실적으로) 📖"},
        {"field": "특징", "prompt": "캐릭터의 특징은? (10자 이상) ✨"},
        {"field": "관계", "prompt": "캐릭터의 관계는? (10자 이상, 없으면 '없음') 👥"},
    ]

    try:
        dm_channel = await user.create_dm()
        await interaction.response.send_message("📬 DM으로 질문 보냈어! 거기서 답해~ 😊", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ DM을 보낼 수 없어! DM 설정을 확인해~ 😅", ephemeral=True)
        return

    question_index = 0
    while question_index < len(questions):
        question = questions[question_index]
        field = question["field"]
        prompt = question["prompt"]
        condition = question.get("condition", lambda x: True)

        if not condition(character_data):
            question_index += 1
            continue

        if field == "사용 기술/마법/요력 추가 여부" and len(skills) >= MAX_SKILLS:
            question_index += 1
            continue

        await dm_channel.send(prompt)

        def check(m):
            return m.author == user and m.channel == dm_channel

        try:
            message = await bot.wait_for("message", check=check, timeout=TIMEOUT_SECONDS)
            answer = message.content.strip()
        except asyncio.TimeoutError:
            await dm_channel.send(f"❌ {TIMEOUT_SECONDS}초 동안 답이 없어! 다시 시작하려면 /캐릭터신청 입력해~ 😊")
            return

        is_valid, error_message = await validate_answer(field, answer, character_data)
        if not is_valid:
            await dm_channel.send(error_message)
            continue

        if field == "사용 기술/마법/요력":
            current_skill["name"] = answer
        elif field == "사용 기술/마법/요력의 위력":
            current_skill["power"] = answer
        elif field == "사용 기술/마법/요력 설명":
            current_skill["description"] = answer
            skills.append(current_skill.copy())
            character_data["사용 기술/마법/요력"] = skills
            current_skill = {}
        elif field == "사용 기술/마법/요력 추가 여부":
            if answer == "아니오":
                question_index += 1
            else:
                question_index = questions.index({"field": "사용 기술/마법/요력", "prompt": "사용 기술/마법/요력 이름은? (50자 이내) 🔥"})
            continue
        else:
            character_data[field] = answer

        question_index += 1

    attributes = ["체력", "지능", "이동속도", "힘"]
    total = sum(int(character_data.get(attr, 0)) for attr in attributes)
    race = character_data.get("종족", "")
    if race == "인간" and not (5 <= total <= 16):
        await dm_channel.send(f"❌ 인간 속성 합산 {total}? 5~16으로 맞춰! 다시 처음부터~ 😅")
        return
    if race == "마법사" and not (5 <= total <= 17):
        await dm_channel.send(f"❌ 마법사 속성 합산 {total}? 5~17로 맞춰! 다시 처음부터~ 😅")
        return
    if race == "요괴" and not (5 <= total <= 18):
        await dm_channel.send(f"❌ 요괴 속성 합산 {total}? 5~18로 맞춰! 다시 처음부터~ 😅")
        return

    prompt = await create_openai_prompt(character_data, guild.id)
    description = "\n".join([f"{k}: {v}" for k, v in character_data.items() if k != "사용 기술/마법/요력"] + [
        f"사용 기술/마법/요력: {skill['name']} (위력: {skill['power']}) {skill['description']}" for skill in character_data.get("사용 기술/마법/요력", [])
    ])

    await queue_batch_task(
        character_id, description, str(user.id), str(dm_channel.id), None, "character_check", prompt
    )
    await dm_channel.send("⏳ 심사 중! 곧 결과 알려줄게~ 😊")

    # 캐릭터 목록 채널에 포스트 (Batch 처리 후 이동)
    list_channel = discord.utils.get(guild.text_channels, name=CHARACTER_LIST_CHANNEL)
    if not list_channel:
        await dm_channel.send(f"❌ '{CHARACTER_LIST_CHANNEL}' 채널을 못 찾았어! 관리자 문의~ 😅")
        return

# 명령어 정의
@bot.tree.command(name="캐릭터신청", description="새 캐릭터를 신청해! DM으로 질문 보낼게~ 😊")
async def character_apply(interaction: discord.Interaction):
    can_proceed, error_message = await check_cooldown(str(interaction.user.id))
    if not can_proceed:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    await character_application(interaction)

@bot.event
async def on_ready():
    await init_db()
    logger.info(f'Bot logged in: {bot.user}')
    try:
        synced = await bot.tree.sync()
        logger.info(f"명령어 동기화 완료: {len(synced)}개 명령어 등록")
    except Exception as e:
        logger.error(f"명령어 동기화 실패: {str(e)}")
    bot.loop.create_task(process_batch())

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN이 .env 파일에 없어!")
        raise ValueError("DISCORD_TOKEN이 .env 파일에 없어!")
    bot.run(DISCORD_TOKEN)
