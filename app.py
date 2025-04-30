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
        await db.commit()

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

# OpenAI로 최종 검증
async def validate_with_openai(character_data, guild_id):
    allowed_roles = ALLOWED_ROLES
    description = "\n".join([f"{k}: {v}" for k, v in character_data.items()])
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
        return pass_status, reason, role_name
    except Exception as e:
        logger.error(f"OpenAI error: {str(e)}")
        return False, f"OpenAI 오류: {str(e)}", None

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

    # DM 채널 생성
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

        # 조건부 질문 스킵
        if not condition(character_data):
            question_index += 1
            continue

        # 기술 추가 여부 처리
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
            await dm_channel.send(f"❌ {TIMEOUT_SECONDS}초 동안 답이 없어! 다시 시작하려면 /캐릭터_신청 입력해~ 😊")
            return

        # 답변 검증
        is_valid, error_message = await validate_answer(field, answer, character_data)
        if not is_valid:
            await dm_channel.send(error_message)
            continue

        # 기술 관련 처리
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

    # 속성 합산 검증
    attributes = ["체력", "지능", "이동속도", "힘"]
    total = sum(int(character_data.get(attr, 0)) for attr in attributes)
    race = character_data.get("종족", "")
    if race == "인간" and not (5 <= total <= 18):
        await dm_channel.send(f"❌ 인간 속성 합산 {total}? 5~16으로 맞춰! 다시 처음부터~ 😅")
        return
    if race == "마법사" and not (5 <= total <= 19):
        await dm_channel.send(f"❌ 마법사 속성 합산 {total}? 5~17로 맞춰! 다시 처음부터~ 😅")
        return
    if race == "요괴" and not (5 <= total <= 20):
        await dm_channel.send(f"❌ 요괴 속성 합산 {total}? 5~18로 맞춰! 다시 처음부터~ 😅")
        return

    # OpenAI 최종 검증
    pass_status, reason, role_name = await validate_with_openai(character_data, guild.id)
    if not pass_status:
        await dm_channel.send(f"❌ 심사 실패: {reason} 수정 후 /캐릭터_신청 다시 시도~ 😊")
        return

    # 역할 부여
    try:
        member = guild.get_member(user.id)
        role = discord.utils.get(guild.roles, name=role_name)
        if role and role not in member.roles:
            await member.add_roles(role)
        race_role_name = character_data["종족"]
        race_role = discord.utils.get(guild.roles, name=race_role_name)
        if race_role and race_role not in member.roles:
            await member.add_roles(race_role)
    except discord.Forbidden:
        await dm_channel.send("❌ 역할 부여 실패! 관리자 권한 확인해~ 😅")

    # 캐릭터 목록 채널에 포스트
    description = f"**유저**: {user.mention}\n"
    for field in character_data:
        if field == "사용 기술/마법/요력":
            description += f"{field}:\n"
            for skill in character_data[field]:
                description += f"- {skill['name']} (위력: {skill['power']}) {skill['description']}\n"
        else:
            description += f"{field}: {character_data[field]}\n"

    try:
        list_channel = discord.utils.get(guild.text_channels, name=CHARACTER_LIST_CHANNEL)
        if list_channel:
            await list_channel.send(description)
        else:
            await dm_channel.send(f"❌ '{CHARACTER_LIST_CHANNEL}' 채널을 못 찾았어! 관리자 문의~ 😅")
    except discord.Forbidden:
        await dm_channel.send(f"❌ '{CHARACTER_LIST_CHANNEL}' 채널에 포스트 권한 없어! 관리자 문의~ 😅")

    # 데이터베이스 저장
    await save_character(character_id, str(user.id), str(guild.id), description, role_name)
    await dm_channel.send(f"🎉 캐릭터 심사 통과! 역할: {role_name} 역극 즐겨~ 🎊")

    # 로그 기록
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"캐릭터 신청 완료\n유저: {user}\n역할: {role_name}\n설명: {description[:100]}...")

# 명령어 정의
@bot.tree.command(name="캐릭터_신청", description="새 캐릭터를 신청해! DM으로 질문 보낼게~ 😊")
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
    await bot.tree.sync()

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    bot.run(DISCORD_TOKEN)
