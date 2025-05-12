import discord
from discord.ext import commands
import asyncio
from discord.ext.commands import CooldownMapping, BucketType
import os
import re
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta
import hashlib
import uuid
from collections import deque
from flask import Flask
import threading
import aiohttp
import io
import asyncpg

# Flask 웹 서버 설정
app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is running!"

# 환경 변수 불러오기
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI API 설정
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# 봇 설정
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
cooldown = CooldownMapping.from_cooldown(1, 5.0, BucketType.user)  # 5초 쿨다운

# 데이터베이스 초기화
async def init_db():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT,
                    guild_id BIGINT,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')
        return pool
    except Exception as e:
        print(f"데이터베이스 초기화 오류: {e}")
        raise 

# 경험치와 레벨 계산
def get_level_xp(level):
    return level * 200  # 레벨당 필요한 경험치

async def add_xp(user_id, guild_id, xp, channel=None, pool=None):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT xp, level FROM users WHERE user_id = $1 AND guild_id = $2',
            user_id, guild_id
        )
        
        if row is None:
            await conn.execute(
                'INSERT INTO users (user_id, guild_id, xp, level) VALUES ($1, $2, $3, 1)',
                user_id, guild_id, xp
            )
            return 1, xp
        
        current_xp, current_level = row['xp'], row['level']
        new_xp = current_xp + xp
        new_level = current_level
        
        while new_xp >= get_level_xp(new_level) and new_level < 30:
            new_xp -= get_level_xp(new_level)
            new_level += 1
            if channel and new_level > current_level:
                levelup_channel = discord.utils.get(channel.guild.channels, name="레벨업")
                if levelup_channel:
                    user = channel.guild.get_member(user_id)
                    await levelup_channel.send(f'{user.mention}님이 레벨 {new_level}로 올라갔어요!')
        
        new_xp = max(0, new_xp)
        
        await conn.execute(
            'UPDATE users SET xp = $1, level = $2 WHERE user_id = $3 AND guild_id = $4',
            new_xp, new_level, user_id, guild_id
        )
        
        return new_level, new_xp

# 메시지 처리
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    bucket = cooldown.get_bucket(message)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        return
    
    xp = len(message.content)
    if xp > 0:
        if hasattr(bot, 'db_pool') and bot.db_pool is not None:
            await add_xp(message.author.id, message.guild.id, xp, message.channel, bot.db_pool)
        else:
            print("db_pool이 아직 준비되지 않았습니다. 다시 시도해주세요.")
    
    await bot.process_commands(message)

# 레벨 확인 명령어
@bot.tree.command(name="레벨", description="현재 레벨과 경험치를 확인해!")
async def level(interaction: discord.Interaction, member: discord.Member = None):
    # 쿨다운 체크
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await send_message_with_retry(interaction, f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()  # 응답 지연 처리
    member = member or interaction.user
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT xp, level FROM users WHERE user_id = $1 AND guild_id = $2',
            member.id, interaction.guild.id
        )
        
        if row is None:
            await send_message_with_retry(interaction, f'{member.display_name}님은 아직 경험치가 없어요!')
        else:
            xp, level = row['xp'], row['level']
            await send_message_with_retry(interaction, f'{member.display_name}님은 현재 레벨 {level}이고, 경험치는 {xp}/{get_level_xp(level)}이에요!')

# 리더보드 명령어
@bot.tree.command(name="리더보드", description="서버의 상위 5명 레벨 랭킹을 확인해!")
async def leaderboard(interaction: discord.Interaction):
    # 쿨다운 체크
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await send_message_with_retry(interaction, f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()  # 응답 지연 처리
    async with bot.db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT user_id, xp, level FROM users WHERE guild_id = $1 ORDER BY level DESC, xp DESC LIMIT 5',
            interaction.guild.id
        )
        
        if not rows:
            await send_message_with_retry(interaction, '아직 리더보드에 데이터가 없어요!')
            return
        
        embed = discord.Embed(title=f"{interaction.guild.name} 리더보드", color=discord.Color.blue())
        for i, row in enumerate(rows, 1):
            user = interaction.guild.get_member(row['user_id'])
            if user:
                embed.add_field(
                    name=f"{i}. {user.display_name}",
                    value=f"레벨 {row['level']} | XP: {row['xp']}/{get_level_xp(row['level'])}",
                    inline=False
                )
        
        await send_message_with_retry(interaction, embed=embed)

# 경험치 추가 명령어 (관리자 전용)
@bot.tree.command(name="경험치추가", description="관리실에서 경험치를 추가해! (관리자 전용)")
@commands.has_permissions(administrator=True)  # 관리자 권한 체크
async def add_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    # 쿨다운 체크
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await send_message_with_retry(interaction, f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()  # 응답 지연 처리
    if interaction.channel.name != "관리실":
        await send_message_with_retry(interaction, "이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
        return
    
    if xp <= 0:
        await send_message_with_retry(interaction, "추가할 경험치는 양수여야 합니다!", ephemeral=True)
        return
        
    new_level, new_xp = await add_xp(member.id, interaction.guild.id, xp, interaction.channel)
    await send_message_with_retry(interaction, f'{member.display_name}님에게 {xp}만큼의 경험치를 추가했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# 경험치 제거 명령어 (관리자 전용)
@bot.tree.command(name="경험치제거", description="관리실에서 경험치를 제거해! (관리자 전용)")
@commands.has_permissions(administrator=True)  # 관리자 권한 체크
async def remove_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    # 쿨다운 체크
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await send_message_with_retry(interaction, f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()  # 응답 지연 처리
    if interaction.channel.name != "관리실":
        await send_message_with_retry(interaction, "이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
        return
    
    if xp <= 0:
        await send_message_with_retry(interaction, "제거할 경험치는 양수여야 합니다!", ephemeral=True)
        return
        
    new_level, new_xp = await add_xp(member.id, interaction.guild.id, -xp, interaction.channel)
    await send_message_with_retry(interaction, f'{member.display_name}님에게서 {xp}만큼의 경험치를 제거했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# --- 두 번째 스크립트: 캐릭터 심사 시스템 ---

# 상수 정의
BANNED_WORDS = ["악마", "천사", "이세계", "드래곤"]
MIN_LENGTH = 50
REQUIRED_FIELDS = ["이름:", "나이:", "성격:"]
COOLDOWN_SECONDS = 5
MAX_REQUESTS_PER_DAY = 1000
RATE_LIMIT_DELAY = 1.0

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
3.캐릭터가 설정과 스탯표에 부합하는지 판단해.
**간결하게 50자 이내로 답변해.**

**규칙**:
- 금지 단어: {banned_words} (이미 확인됨).
- 필수 항목: {required_fields} (이미 확인됨).
- 허용 종족: {allowed_races}.
- 속성: 체력, 지능, 이동속도, 힘(1~6), 냉철(1~4), 기술/마법/요력 위력(1~6) (이미 확인됨).
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
- 기술/마법/요력의 위력이 4이면 쿨타임이 15 이상이어야 함.
- 기술/마법/요력의 위력이 5이면 쿨타임이 20 이상이어야 함.
- 기술/마법/요력의 위력이 6이면 쿨타임이 40 이상이어야 함.
- 기술/마법/요력의 지속 시간은 39를 넘으면 안됨.
- 기술/마법/요력의 쿨타임과 지속시간의 단위가 '지문'이라면 초로 해석.
- 스탯표 참고해서 기술/마법/요력의 설명 보기.
- 설정 참고해서 과거사 보기.
- 만약 종족이 요괴인데 AML이면 안된다.(과거사나 특징에서 요괴 정체를 숨기고 있는 것이라면 통과).
- 만약 기술/마법/요력이 장비 혹은 무기라면 지속 시간과 쿨타임이 양식을 어긋나도 통과.
- 키/몸무게에 cm/kg 단위가 없어도 cm/kg으로 해석.(예:180/80,180cm/80kg)
- 키/몸무게에 300m 이상의 키와 10000kg 이상의 몸무게는 안된다.
- 학년 및 반은 학년별로 3반이 최대이다.
- 종족 중 요괴는 소속의 학생과 선생님은 가능하다.
- 소속이 AML을 제외하면 학생과 선생님은 종족을 제외하면 학교 관련 캐릭터만 통과지만 학교 관련 캐릭터가 아닌 소속의 직접적 묘사가 없으면 통과.(군인이나 특수부대 등은 불가)

**역할 판단**:
1. 소속에 'AML' 포함 → AML.
2. 소속에 '선생'/'선생님' 포함 → 선생님.
3. 소속에 '학생' 포함 → 학생.
4. 모호하면 실패.

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
- AML은 요괴와 마법사를 증오하고 하람고를 적대, 갈등 조장하는 단체.

**스탯표**:
목적: 캐릭터 스탯 시스템 설계. 인간~초인 스케일.
스탯: 지능, 힘, 속도, 체력, 냉철, 능력.
각 항목 1~7 (능력만 1~9). 기준은 아래 표 참조.

지능
1 = IQ 60~80, 2 = 90, 3 = 100, 4 = 120, 5 = 150, 6 = 180  

힘
1 = 29kg 이하, 2 = 30kg, 3 = 50kg, 4 = 125kg, 5 = 300kg, 6 = 600kg  

이동속도
1 = 100m in 41, 2 = 40~26s, 3 = 25–20s, 4 = 19~13s, 5 = 12–6s, 6 = 5~3s  

냉철
1 = 원초적 감정, 2 = 평범한 청소년, 3 = 격한 감정 무시, 4 = 감정 동요 없음

체력
1 = 간신히 생존, 2 = 운동 부족, 3 = 평범한 청소년, 4 = 운동선수, 5 = 초인적 맷집, 6 = 인간 한계 초월

능력/마법/기술 위력
1 = 일반인에게 피해 없음, 2 = 일반인에게 경미한 상처, 3 = 일반인에게 깊은 상처, 4 = 작은 콘크리트 파괴, 5 = 큰 콘크리트 파괴, 6 = 작은 건물 파괴

**캐릭터 설명**:
{description}

**응답 형식**:
- 통과: "✅ 역할: [역할]"
- 실패: "❌ [실패 이유]"
"""

# 질문 목록
questions = [
    {"field": "포스트 이름", "prompt": "포스트 이름을 입력해주세요.(향후 수정 명령어 시 이 질문에 작성한 이름을 작성해야합니다!)", "validator": lambda x: len(x) > 0, "error_message": "포스트 이름을 입력해주세요."},
    {"field": "종족", "prompt": "종족을 선택해주세요.", "options": ["인간", "마법사", "요괴"], "error_message": "허용되지 않은 종족입니다. 인간, 마법사, 요괴 중에서 선택해주세요."},
    {"field": "이름", "prompt": "캐릭터의 이름을 입력해주세요.", "validator": lambda x: len(x) > 0, "error_message": "이름을 입력해주세요."},
    {"field": "성별", "prompt": "성별을 선택해주세요.", "options": ["남", "여", "불명"], "error_message": "허용되지 않은 성별입니다. 남, 여, 불명 중에서 선택해주세요."},
    {"field": "나이", "prompt": "나이를 입력해주세요. (1~5000)", "validator": lambda x: x.isdigit() and 1 <= int(x) <= 5000, "error_message": "나이는 1에서 5000 사이의 숫자여야 합니다."},
    {"field": "키/몸무게", "prompt": "키와 몸무게를 입력해주세요. (예: 170cm/60kg)", "validator": lambda x: True, "error_message": ""},
    {"field": "성격", "prompt": "성격을 설명해주세요. (최소 10자)", "validator": lambda x: len(x) >= 10, "error_message": "성격 설명이 너무 짧습니다. 최소 10자 이상 입력해주세요."},
    {"field": "외모", "prompt": "외모를 설명(최소 20자)하거나 이미지를 업로드해주세요.", "validator": lambda x: (len(x) >= 20 if isinstance(x, str) and not x.startswith("이미지_") else True), "error_message": "외모 설명이 너무 짧습니다. 최소 20자 이상 입력하거나 이미지를 업로드해주세요."},
    {"field": "소속", "prompt": "소속을 선택해주세요.", "options": ["학생", "선생님", "A.M.L"], "error_message": "허용되지 않은 소속입니다. 학생, 선생님, A.M.L 중에서 선택해주세요."},
    {"field": "학년 및 반", "prompt": "학년과 반을 입력해주세요. (예: 1학년 2반, 1-2반, 1/2반)", "validator": lambda x: re.match(r"^\d[-/]\d반$|^\d학년\s*\d반$", x), "error_message": "학년과 반은 'x-y반', 'x학년 y반', 'x/y반' 형식으로 입력해주세요.", "condition": lambda answers: answers.get("소속") == "학생"},
    {"field": "담당 과목 및 학년, 반", "prompt": "담당 과목과 학년, 반을 입력해주세요. (예: 수학, 1학년 2반)", "validator": lambda x: len(x) > 0, "error_message": "담당 과목과 학년, 반을 입력해주세요.", "condition": lambda answers: answers.get("소속") == "선생님"},
    {"field": "체력", "prompt": "체력 수치를 선택해주세요.", "options": ["1", "2", "3", "4", "5", "6"], "error_message": "체력은 1에서 6 사이의 숫자여야 합니다."},
    {"field": "지능", "prompt": "지능 수치를 선택해주세요.", "options": ["1", "2", "3", "4", "5", "6"], "error_message": "지능은 1에서 6 사이의 숫자여야 합니다."},
    {"field": "이동속도", "prompt": "이동속도 수치를 선택해주세요.", "options": ["1", "2", "3", "4", "5", "6"], "error_message": "이동속도는 1에서 6 사이의 숫자여야 합니다."},
    {"field": "힘", "prompt": "힘 수치를 선택해주세요.", "options": ["1", "2", "3", "4", "5", "6"], "error_message": "힘은 1에서 6 사이의 숫자여야 합니다."},
    {"field": "냉철", "prompt": "냉철 수치를 선택해주세요.", "options": ["1", "2", "3", "4"], "error_message": "냉철은 1에서 4 사이의 숫자여야 합니다."},
    {"field": "사용 기술/마법/요력", "prompt": "사용 기술/마법/요력의 이름을 입력해주세요.", "validator": lambda x: len(x) > 0, "error_message": "사용 기술/마법/요력을 입력해주세요.", "is_tech": True},
    {"field": "사용 기술/마법/요력 위력", "prompt": "사용 기술/마법/요력의 위력을 선택해주세요.", "options": ["1", "2", "3", "4", "5", "6"], "error_message": "위력은 1에서 6 사이의 숫자여야 합니다.", "is_tech": True},
    {"field": "사용 기술/마법/요력 쿨타임", "prompt": "사용 기술/마법/요력의 쿨타임을 입력해주세요. (예: 30초, 최소 위력 4는 15초, 위력 5는 20초, 위력 6은 40초로 해주세요.)", "validator": lambda x: len(x) > 0, "error_message": "쿨타임을 입력해주세요.", "is_tech": True},
    {"field": "사용 기술/마법/요력 지속시간", "prompt": "사용 기술/마법/요력의 지속시간을 입력해주세요. (예: 10초, 할퀴기나 주먹같은 단발 공격은 1초로 해주세요)", "validator": lambda x: len(x) > 0, "error_message": "지속시간을 입력해주세요.", "is_tech": True},
    {"field": "사용 기술/마법/요력 설명", "prompt": "사용 기술/마법/요력을 설명해주세요. (최소 20자)", "validator": lambda x: len(x) >= 20, "error_message": "설명이 너무 짧습니다. 최소 20자 이상 입력해주세요.", "is_tech": True},
    {"field": "사용 기술/마법/요력 추가 여부", "prompt": "기술/마법/요력을 추가하시겠습니까?", "options": ["예", "아니요"], "error_message": "예 또는 아니요로 선택해주세요."},
    {"field": "과거사", "prompt": "과거사를 설명해주세요. (최소 20자)", "validator": lambda x: len(x) >= 20, "error_message": "과거사 설명이 너무 짧습니다. 최소 20자 이상 입력해주세요."},
    {"field": "특징", "prompt": "특징을 설명해주세요. (최소 10자)", "validator": lambda x: len(x) >= 10, "error_message": "특징 설명이 너무 짧습니다. 최소 10자 이상 입력해주세요."},
    {"field": "관계", "prompt": "관계를 설명해주세요. (없으면 '없음' 입력)", "validator": lambda x: True, "error_message": ""},
]

# 수정 가능한 항목 목록
EDITABLE_FIELDS = [q["field"] for q in questions if q["field"] != "사용 기술/마법/요력 추가 여부"]

# 메모리 내 저장소
flex_queue = deque()
character_storage = {}
cooldown_storage = {}
flex_tasks = {}

# 서버별 설정 조회
async def get_settings(guild_id):
    return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME

# 서버별 프롬프트 조회
async def get_prompt(guild_id, allowed_roles):
    return DEFAULT_PROMPT.format(
        banned_words=', '.join(BANNED_WORDS),
        required_fields=', '.join(REQUIRED_FIELDS),
        allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
        allowed_roles=', '.join(allowed_roles),
        description="{description}"
    )

# 메모리 기반 쿨다운 및 요청 횟수 체크
async def check_cooldown(user_id):
    now = datetime.utcnow()
    if user_id not in cooldown_storage:
        cooldown_storage[user_id] = {
            "last_request": now,
            "request_count": 1,
            "reset_date": now.date()
        }
        return True, ""
    
    user_data = cooldown_storage[user_id]
    last_request = user_data["last_request"]
    request_count = user_data["request_count"]
    reset_date = user_data["reset_date"]

    if reset_date < now.date():
        user_data["request_count"] = 0
        user_data["reset_date"] = now.date()
        request_count = 0

    if request_count >= MAX_REQUESTS_PER_DAY:
        return False, f"❌ 하루 최대 {MAX_REQUESTS_PER_DAY}번이야! 내일 다시 와~ 😊"
    
    if (now - last_request).total_seconds() < COOLDOWN_SECONDS:
        return False, f"❌ {COOLDOWN_SECONDS}초 더 기다려야 해~ 😅"

    user_data["last_request"] = now
    user_data["request_count"] = request_count + 1
    return True, ""

# 추가 검증 함수
def validate_all(answers):
    errors = []
    race = answers.get("종족")
    attributes = []
    for attr in ["체력", "지능", "이동속도", "힘", "냉철"]:
        try:
            value = int(answers.get(attr, 0))
            attributes.append(value)
        except (ValueError, TypeError):
            errors.append(([attr], f"{attr}은 숫자여야 합니다."))
            return errors
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
    
    for i in range(tech_count):
        power_field = f"사용 기술/마법/요력 위력_{i}"
        cooldown_field = f"사용 기술/마법/요력 쿨타임_{i}"
        duration_field = f"사용 기술/마법/요력 지속시간_{i}"
        try:
            power = int(answers.get(power_field, 0))
            cooldown = answers.get(cooldown_field, "")
            duration = answers.get(duration_field, "")
            
            cooldown_value = float(re.findall(r"\d+", cooldown)[0]) if re.findall(r"\d+", cooldown) else 0
            if power == 4 and cooldown_value < 15:
                errors.append(([cooldown_field], "위력 4의 기술은 쿨타임이 15초 이상이어야 합니다."))
            elif power == 5 and cooldown_value < 20:
                errors.append(([cooldown_field], "위력 5의 기술은 쿨타임이 20초 이상이어야 합니다."))
            elif power == 6 and cooldown_value < 40:
                errors.append(([cooldown_field], "위력 6의 기술은 쿨타임이 40초 이상이어야 합니다."))
            
            duration_value = float(re.findall(r"\d+", duration)[0]) if re.findall(r"\d+", duration) else 0
            if duration_value > 39:
                errors.append(([duration_field], "기술의 지속 시간은 39초를 초과할 수 없습니다."))
        except (ValueError, IndexError):
            errors.append(([power_field, cooldown_field, duration_field], "기술의 위력, 쿨타임, 지속 시간이 올바른 형식이 아닙니다."))
    
    return errors

# 캐릭터 심사 결과 저장
character_storage_lock = asyncio.Lock()

async def save_result(character_id, description, pass_status, reason, role_name, user_id, character_name, race, age, gender, thread_id, post_name):
    async with character_storage_lock:
        description_hash = hashlib.md5(description.encode()).hexdigest()
        timestamp = datetime.utcnow().isoformat()
        character_storage[character_id] = {
            "character_id": character_id,
            "description_hash": description_hash,
            "pass": pass_status,
            "reason": reason,
            "role_name": role_name,
            "user_id": user_id,
            "character_name": character_name,
            "race": race,
            "age": age,
            "gender": gender,
            "thread_id": thread_id,
            "description": description,
            "timestamp": timestamp,
            "post_name": post_name
        }

# 캐릭터 심사 결과 조회
async def get_result(description):
    description_hash = hashlib.md5(description.encode()).hexdigest()
    for char in character_storage.values():
        if char["description_hash"] == description_hash:
            return char["pass"], char["reason"], char["role_name"]
    return None

# 사용자별 캐릭터 조회
async def find_characters_by_post_name(post_name, user_id):
    result = []
    for char in character_storage.values():
        if char["pass"] and char["user_id"] == user_id and char["post_name"].lower() == post_name.lower():
            result.append((
                char["character_id"],
                char["character_name"],
                char["race"],
                char["age"],
                char["gender"],
                char["thread_id"],
                char["post_name"]
            ))
    return result

# 캐릭터 정보 조회
async def get_character_info(character_id):
    char = character_storage.get(character_id)
    if char:
        answers = {}
        for line in char["description"].split("\n"):
            if ": " in line:
                key, value = line.split(": ", 1)
                answers[key] = value
        return answers
    return None

# Flex 작업 큐에 추가
flex_queue_event = asyncio.Event()

async def queue_flex_task(character_id, description, user_id, channel_id, thread_id, task_type, prompt):
    task_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    flex_tasks[task_id] = {
        "task_id": task_id,
        "character_id": character_id,
        "description": description,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "type": task_type,
        "prompt": prompt,
        "status": "pending",
        "created_at": created_at
    }
    flex_queue.append(task_id)
    flex_queue_event.set()
    return task_id

# 429 에러 재시도 로직
async def send_message_with_retry(interaction_or_channel, content=None, max_retries=3, ephemeral=False, view=None, files=None, embed=None, is_interaction=False, **kwargs):
    for attempt in range(max_retries):
        try:
            if is_interaction and isinstance(interaction_or_channel, discord.Interaction):
                if interaction_or_channel.response.is_done():
                    await interaction_or_channel.followup.send(content, ephemeral=ephemeral, view=view, files=files, embed=embed)
                else:
                    await interaction_or_channel.response.send_message(content, ephemeral=ephemeral, view=view, files=files, embed=embed)
            else:
                await interaction_or_channel.send(content, view=view, files=files, embed=embed)
            return
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, 'retry_after', 5.0)
                print(f"429 Rate Limit Error, retrying after {retry_after} seconds...")
                await asyncio.sleep(retry_after)
            else:
                print(f"Error in send_message_with_retry: {str(e)}")
                raise
        except Exception as e:
            print(f"Unexpected error in send_message_with_retry: {str(e)}")
            raise
    raise discord.HTTPException(response=None, message="최대 재시도 횟수 초과")

# Flex 작업 처리
async def process_flex_queue():
    while True:
        if not flex_queue:
            await flex_queue_event.wait()
            flex_queue_event.clear()
        task_id = flex_queue.popleft()
        task = flex_tasks.get(task_id)
        if not task or task["status"] != "pending":
            continue

        try:
            response = openai_client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[{"role": "user", "content": task["prompt"]}],
                max_tokens=50
            )
            result = response.choices[0].message.content.strip()
            pass_status = result.startswith("✅")
            role_name = result.split("역할: ")[1] if pass_status and "역할: " in result else None
            reason = result[2:] if not pass_status else "통과"

            answers = {}
            for line in task["description"].split("\n"):
                if ": " in line:
                    key, value = line.split(": ", 1)
                    answers[key] = value
            character_name = answers.get("이름")
            race = answers.get("종족")
            age = answers.get("나이")
            gender = answers.get("성별")
            post_name = answers.get("포스트 이름")

            channel = bot.get_channel(int(task["channel_id"]))
            guild = channel.guild
            member = guild.get_member(int(task["user_id"]))

            files = []
            appearance = answers.get("외모", "")
            if appearance.startswith("이미지_"):
                image_url = appearance[len("이미지_"):]
                if image_url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(image_url, timeout=10) as response:
                            if response.status == 200:
                                content = await response.read()
                                files.append(discord.File(fp=io.BytesIO(content), filename="appearance.png"))
                            else:
                                print(f"Failed to download image: HTTP {response.status}")

            result_message = ""
            if pass_status:
                allowed_roles, _ = await get_settings(guild.id)
                if role_name and role_name not in allowed_roles:
                    result_message = f"❌ 역할 `{role_name}`은 허용되지 않아! 허용된 역할: {', '.join(allowed_roles)} 🤔"
                    pass_status = False
                else:
                    has_role = False
                    role = discord.utils.get(guild.roles, name=role_name) if role_name else None
                    race_role = discord.utils.get(guild.roles, name=race) if race else None
                    if role and role in member.roles:
                        has_role = True
                    if race_role and race_role in member.roles:
                        has_role = True

                    if has_role:
                        result_message = "🎉 이미 역할이 있어! 마음껏 즐겨~ 🎊"
                    else:
                        if role:
                            await member.add_roles(role)
                            result_message += f" (역할 `{role_name}` 부여했어! 😊)"
                        if race_role:
                            await member.add_roles(race_role)
                            result_message += f" (종족 `{race}` 부여했어! 😊)"

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
                                tech_cooldown = answers.get(f"사용 기술/마법/요력 쿨타임_{i}", "미기재")
                                tech_duration = answers.get(f"사용 기술/마법/요력 지속시간_{i}", "미기재")
                                tech_desc = answers.get(f"사용 기술/마법/요력 설명_{i}", "미기재")
                                techs.append(f"<{tech_name}> (위력: {tech_power}, 쿨타임: {tech_cooldown}, 지속시간: {tech_duration})\n설명: {tech_desc}")
                        formatted_description += "사용 기술/마법/요력:\n" + "\n\n".join(techs) + "\n" if techs else "사용 기술/마법/요력:\n없음\n"
                        formatted_description += "\n"
                        formatted_description += (
                            f"과거사: {answers.get('과거사', '미기재')}\n"
                            f"특징: {answers.get('특징', '미기재')}\n\n"
                            f"관계: {answers.get('관계', '미기재')}"
                        )

                        char_channel = discord.utils.get(guild.channels, name="캐릭터-목록")
                        if not char_channel:
                            print("Error: 캐릭터-목록 채널을 찾을 수 없습니다.")
                            result_message += "\n❌ 캐릭터-목록 채널을 못 찾았어! 서버 관리자에게 문의해~ 🥺"
                        else:
                            print(f"Found 캐릭터-목록 channel: {char_channel.name} (ID: {char_channel.id}, Type: {type(char_channel).__name__})")
                            try:
if isinstance(char_channel, discord.ForumChannel):
    thread_name = f"캐릭터: {post_name}"[:100]
    thread, message = await char_channel.create_thread(
        name=thread_name,
        content=f"{member.mention}의 캐릭터:\n{formatted_description}",
        files=files
    )
    task["thread_id"] = str(thread.id)
    print(f"Posted to ForumChannel thread: {thread.id}")
else:
    message = await send_message_with_retry(
        char_channel,
        f"{member.mention}의 캐릭터:\n{formatted_description}",
        files=files
    )
    task["thread_id"] = str(message.id)
    print(f"Posted to TextChannel message: {message.id}")
                            except Exception as e:
                                print(f"Error posting to 캐릭터-목록 channel: {str(e)}")
                                result_message += f"\n❌ 캐릭터-목록 채널 등록 중 오류: {str(e)} 🥺"
            else:
                failed_fields = []
                for field in answers:
                    if field in reason:
                        failed_fields.append(field)
                result_message += f"\n다시 입력해야 할 항목: {', '.join(failed_fields) if failed_fields else '알 수 없음'}"

            await save_result(
                task["character_id"],
                task["description"],
                pass_status,
                reason,
                role_name,
                task["user_id"],
                character_name,
                race,
                age,
                gender,
                task["thread_id"],
                post_name
            )
            await send_message_with_retry(channel, f"{member.mention} {result_message}")
            task["status"] = "completed"

        except Exception as e:
            print(f"Error processing flex task: {str(e)}")
            await send_message_with_retry(channel, f"❌ 오류야! {str(e)} 다시 시도해~ 🥹")
            task["status"] = "failed"
        await asyncio.sleep(1)

# 버튼 뷰 클래스
class SelectionView(discord.ui.View):
    def __init__(self, options, field, user, callback):
        super().__init__(timeout=600.0)
        self.options = options
        self.field = field
        self.user = user
        self.callback = callback
        self.message = None
        for option in options:
            button = discord.ui.Button(label=option, style=discord.ButtonStyle.primary)
            button.callback = self.create_button_callback(option)
            self.add_item(button)

    def create_button_callback(self, option):
        async def button_callback(interaction: discord.Interaction):
            if interaction.user != self.user:
                await interaction.response.send_message("이 버튼은 당신이 사용할 수 없어요!", ephemeral=True)
                return
            await interaction.response.send_message(f"{option}을(를) 선택했어!", ephemeral=True)
            await self.callback(option)
            self.stop()
        return button_callback

    async def on_timeout(self):
        if self.message:
            await self.message.channel.send(f"{self.user.mention} ❌ 10분 동안 응답이 없어 신청이 취소됐어요. /캐릭터_신청 명령어로 다시 시도해주세요! 🥹")
        else:
            channel = bot.get_channel(self.user.dm_channel.id if self.user.dm_channel else self.user.id)
            if channel:
                await channel.send(f"{self.user.mention} ❌ 10분 동안 응답이 없어 신청이 취소됐어요. /캐릭터_신청 명령어로 다시 시도해주세요! 🥹")

# 캐릭터 신청 명령어
@bot.tree.command(name="캐릭터_신청", description="캐릭터를 신청해! 순차적으로 질문에 답해줘~")
async def character_apply(interaction: discord.Interaction):
    user = interaction.user
    channel = interaction.channel
    answers = {}

    can_proceed, error_message = await check_cooldown(str(user.id))
    if not can_proceed:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    await interaction.response.send_message("✅ 캐릭터 신청 시작! 질문에 하나씩 답해줘~ 😊", ephemeral=True)

    async def handle_selection(field, option):
        nonlocal answers
        answers[field] = option

    for question in questions:
        if not question.get("is_tech") and question["field"] != "사용 기술/마법/요력 추가 여부":
            if question.get("condition") and not question["condition"](answers):
                continue
            while True:
                if question.get("options"):
                    view = SelectionView(question["options"], question["field"], user, lambda option: handle_selection(question["field"], option))
                    message, _ = await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                    view.message = message
                    await view.wait()
                    if question["field"] not in answers:
                        return
                    break
                else:
                    await send_message_with_retry(channel, f"{user.mention} {question['prompt']}")
                    def check(m):
                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                    try:
                        response = await bot.wait_for("message", check=check, timeout=600.0)
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
                        return

    tech_counter = 0
    while tech_counter < 6:
        for tech_question in questions:
            if tech_question.get("is_tech"):
                field = f"{tech_question['field']}_{tech_counter}"
                while True:
                    if tech_question.get("options"):
                        view = SelectionView(tech_question["options"], field, user, lambda option: handle_selection(field, option))
                        message, _ = await send_message_with_retry(channel, f"{tech_question['prompt']}", view=view)
                        view.message = message
                        await view.wait()
                        if field not in answers:
                            return
                        break
                    else:
                        await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                        def check(m):
                            return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                        try:
                            response = await bot.wait_for("message", check=check, timeout=600.0)
                            tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                            if tech_question["validator"](tech_answer):
                                answers[field] = tech_answer
                                break
                            else:
                                await send_message_with_retry(channel, tech_question["error_message"])
                        except asyncio.TimeoutError:
                            await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 신청 취소됐어! 다시 시도해~ 🥹")
                            return
        if tech_counter < 5:
            add_more_view = SelectionView(["예", "아니요"], "사용 기술/마법/요력 추가 여부", user, lambda option: handle_selection("사용 기술/마법/요력 추가 여부", option))
            message, _ = await send_message_with_retry(channel, f"{user.mention} 기술/마법/요력을 추가하시겠습니까?", view=add_more_view)
            add_more_view.message = message
            await add_more_view.wait()
            if "사용 기술/마법/요력 추가 여부" not in answers or answers["사용 기술/마법/요력 추가 여부"] != "예":
                break
        tech_counter += 1

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
                    view = SelectionView(question["options"], question["field"], user, lambda option: handle_selection(question["field"], option))
                    message, _ = await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                    view.message = message
                    await view.wait()
                    if question["field"] not in answers:
                        return
                else:
                    await send_message_with_retry(channel, f"{user.mention} {field}을 다시 입력해: {question['prompt']}")
                    def check(m):
                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                    try:
                        response = await bot.wait_for("message", check=check, timeout=600.0)
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
                        return

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
    await queue_flex_task(character_id, description, str(user.id), str(channel.id), None, "character_check", prompt)
    await save_result(character_id, description, False, "심사 중", None, str(user.id), answers.get("이름"), answers.get("종족"), answers.get("나이"), answers.get("성별"), None, answers.get("포스트 이름"))
    await send_message_with_retry(channel, f"{user.mention} ⏳ 심사 중이야! 곧 결과 알려줄게~ 😊", is_interaction=True, interaction=interaction)

# 캐릭터 수정 명령어
@bot.tree.command(name="캐릭터_수정", description="등록된 캐릭터를 수정해! 포스트 이름을 입력해줘~")
async def character_edit(interaction: discord.Interaction, post_name: str):
    user = interaction.user
    channel = interaction.channel

    can_proceed, error_message = await check_cooldown(str(user.id))
    if not can_proceed:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    characters = await find_characters_by_post_name(post_name, str(user.id))
    if not characters:
        await interaction.response.send_message(f"{user.mention} ❌ '{post_name}'에 해당하는 포스트가 없어! /캐릭터_신청으로 등록해줘~ 🥺", ephemeral=True)
        return

    selected_char = characters[0]
    character_id, _, _, _, _, thread_id, _ = selected_char
    answers = await get_character_info(character_id)
    if not answers:
        await interaction.response.send_message(f"{user.mention} ❌ 캐릭터 정보를 불러올 수 없어! 다시 시도해~ 🥹", ephemeral=True)
        return

    answers["포스트 이름"] = post_name
    await interaction.response.send_message(f"✅ '{post_name}' 수정 시작! 수정할 항목 번호를 쉼표로 구분해 입력해줘~", ephemeral=True)
    fields_list = "\n".join([f"{i+1}. {field}" for i, field in enumerate(EDITABLE_FIELDS)])
    await send_message_with_retry(channel, f"{user.mention} 수정할 항목 번호를 쉼표로 구분해 입력해줘 (예: 1,3,5). 기술/마법/요력 수정은 16번 선택!\n{fields_list}")

    try:
        response = await bot.wait_for(
            "message",
            check=lambda m: m.author == user and m.channel == channel,
            timeout=600.0
        )
        selected_indices = [int(i.strip()) - 1 for i in response.content.split(",")]
        if not all(0 <= i < len(EDITABLE_FIELDS) for i in selected_indices):
            await send_message_with_retry(channel, f"{user.mention} ❌ 유효한 번호를 입력해줘! 다시 시도해~ 🥹")
            return
    except (ValueError, asyncio.TimeoutError):
        await send_message_with_retry(channel, f"{user.mention} ❌ 잘못된 입력이거나 시간이 초과됐어! 다시 시도해~ 🥹")
        return

    async def handle_selection(field, option):
        answers[field] = option

    for index in selected_indices:
        if "사용 기술/마법/요력" in EDITABLE_FIELDS[index]:
            continue
        question = next(q for q in questions if q["field"] == EDITABLE_FIELDS[index])
        while True:
            if question.get("options"):
                view = SelectionView(question["options"], question["field"], user, lambda option: handle_selection(question["field"], option))
                message, _ = await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                view.message = message
                await view.wait()
                if question["field"] not in answers:
                    return
                break
            else:
                await send_message_with_retry(channel, f"{user.mention} {question['field']}을 수정해: {question['prompt']}")
                def check(m):
                    return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                try:
                    response = await bot.wait_for(
                        "message",
                        check=check,
                        timeout=600.0
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
                    return

    if any("사용 기술/마법/요력" in EDITABLE_FIELDS[i] for i in selected_indices):
        techs = [(k, answers[k], answers.get(f"사용 기술/마법/요력 위력_{k.split('_')[1]}"), answers.get(f"사용 기술/마법/요력 쿨타임_{k.split('_')[1]}"), answers.get(f"사용 기술/마법/요력 지속시간_{k.split('_')[1]}"), answers.get(f"사용 기술/마법/요력 설명_{k.split('_')[1]}"))
                 for k in sorted([k for k in answers if k.startswith("사용 기술/마법/요력_")], key=lambda x: int(x.split('_')[1]))]
        tech_list = "\n".join([f"{i+1}. {t[1]} (위력: {t[2]}, 쿨타임: {t[3]}, 지속시간: {t[4]}, 설명: {t[5]})" for i, t in enumerate(techs)]) if techs else "없음"
        await send_message_with_retry(channel, f"{user.mention} 현재 기술/마법/요력:\n{tech_list}\n수정하려면 번호, 추가하려면 'a', 삭제하려면 'd'로 입력 (예: 1,a,d)")
        try:
            response = await bot.wait_for(
                "message",
                check=lambda m: m.author == user and m.channel == channel,
                timeout=600.0
            )
            actions = [a.strip() for a in response.content.split(",")]
        except asyncio.TimeoutError:
            await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
            return

        for action in actions:
            if action.isdigit():
                idx = int(action) - 1
                if 0 <= idx < len(techs):
                    for tech_question in questions:
                        if tech_question.get("is_tech"):
                            while True:
                                field = f"{tech_question['field']}_{techs[idx][0].split('_')[1]}"
                                if tech_question.get("options"):
                                    view = SelectionView(tech_question["options"], field, user, lambda option: handle_selection(field, option))
                                    message, _ = await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}", view=view)
                                    view.message = message
                                    await view.wait()
                                    if field not in answers:
                                        return
                                else:
                                    await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                                    def check(m):
                                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                                    try:
                                        response = await bot.wait_for(
                                            "message",
                                            check=check,
                                            timeout=600.0
                                        )
                                        tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                                        if tech_question["validator"](tech_answer):
                                            answers[field] = tech_answer
                                            break
                                        else:
                                            await send_message_with_retry(channel, tech_question["error_message"])
                                    except asyncio.TimeoutError:
                                        await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                                        return
            elif action == "a" and len(techs) < 6:
                tech_counter = len(techs)
                for tech_question in questions:
                    if tech_question.get("is_tech"):
                        while True:
                            field = f"{tech_question['field']}_{tech_counter}"
                            if tech_question.get("options"):
                                view = SelectionView(tech_question["options"], field, user, lambda option: handle_selection(field, option))
                                message, _ = await send_message_with_retry(channel, f"{tech_question['prompt']}", view=view)
                                view.message = message
                                await view.wait()
                                if field not in answers:
                                    return
                            else:
                                await send_message_with_retry(channel, f"{user.mention} {tech_question['prompt']}")
                                def check(m):
                                    return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                                try:
                                    response = await bot.wait_for(
                                        "message",
                                        check=check,
                                        timeout=600.0
                                    )
                                    tech_answer = response.content.strip() if response.content.strip() else f"이미지_{response.attachments[0].url}" if response.attachments else ""
                                    if tech_question["validator"](tech_answer):
                                        answers[field] = tech_answer
                                        break
                                    else:
                                        await send_message_with_retry(channel, tech_question["error_message"])
                                except asyncio.TimeoutError:
                                    await send_message_with_retry(channel, f"{user.mention} ❌ 5분 내로 답변 안 해서 수정 취소됐어! 다시 시도해~ 🥹")
                                    return
                tech_counter += 1
            elif action == "d" and techs:
                await send_message_with_retry(channel, f"{user.mention} 삭제할 기술 번호를 입력해줘 (1-{len(techs)})")
                try:
                    response = await bot.wait_for(
                        "message",
                        check=lambda m: m.author == user and m.channel == channel,
                        timeout=600.0
                    )
                    idx = int(response.content.strip()) - 1
                    if 0 <= idx < len(techs):
                        key = techs[idx][0]
                        del answers[key]
                        del answers[f"사용 기술/마법/요력 위력_{key.split('_')[1]}"]
                        del answers[f"사용 기술/마법/요력 쿨타임_{key.split('_')[1]}"]
                        del answers[f"사용 기술/마법/요력 지속시간_{key.split('_')[1]}"]
                        del answers[f"사용 기술/마법/요력 설명_{key.split('_')[1]}"]
                    else:
                        await send_message_with_retry(channel, f"{user.mention} ❌ 유효한 번호를 입력해줘! 다시 시도해~ 🥹")
                except (ValueError, asyncio.TimeoutError):
                    await send_message_with_retry(channel, f"{user.mention} ❌ 잘못된 입력이거나 시간이 초과됐어! 다시 시도해~ 🥹")
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
                    view = SelectionView(question["options"], question["field"], user, lambda option: handle_selection(question["field"], option))
                    message, _ = await send_message_with_retry(channel, f"{user.mention} {question['prompt']}", view=view)
                    view.message = message
                    await view.wait()
                    if question["field"] not in answers:
                        return
                else:
                    await send_message_with_retry(channel, f"{user.mention} {field}을 다시 입력해: {question['prompt']}")
                    def check(m):
                        return m.author == user and m.channel == channel and (m.content.strip() or m.attachments)
                    try:
                        response = await bot.wait_for(
                            "message",
                            check=check,
                            timeout=600.0
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
                        return

    description = "\n".join([f"{field}: {answers[field]}" for field in answers if field != "외모"])
    allowed_roles, _ = await get_settings(interaction.guild.id)
    prompt = DEFAULT_PROMPT.format(
        banned_words=', '.join(BANNED_WORDS),
        required_fields=', '.join(REQUIRED_FIELDS),
        allowed_races=', '.join(DEFAULT_ALLOWED_RACES),
        allowed_roles=', '.join(allowed_roles),
        description=description
    )
    await queue_flex_task(character_id, description, str(user.id), str(channel.id), thread_id, "character_check", prompt)
    await send_message_with_retry(channel, f"{user.mention} ⏳ 수정 심사 중이야! 곧 결과 알려줄게~ 😊", is_interaction=True, interaction=interaction)

# 캐릭터 목록 명령어
@bot.tree.command(name="캐릭터_목록", description="등록된 캐릭터 목록을 확인해!")
async def character_list(interaction: discord.Interaction):
    user = interaction.user
    characters = []
    for char in character_storage.values():
        if char["user_id"] == str(user.id) and char["pass"]:
            characters.append(char)
    if not characters:
        await interaction.response.send_message("등록된 캐릭터가 없어! /캐릭터_신청으로 등록해줘~ 🥺", ephemeral=True)
        return
    char_list = "\n".join([f"- {c['character_name']} (포스트: {c['post_name']})" for c in characters])
    await interaction.response.send_message(f"**너의 캐릭터 목록**:\n{char_list}", ephemeral=True)

# 봇 시작 시 실행
@bot.event
async def on_ready():
    global bot  # Declare bot as global at the start
    print(f'봇이 로그인했어: {bot.user}')
    bot.db_pool = await init_db()  # Supabase 연결
    try:
        synced = await bot.tree.sync()
        print(f'명령어가 동기화되었어: {len(synced)}개의 명령어 등록됨')
    except Exception as e:
        print(f'명령어 동기화 실패: {e}')
    bot.loop.create_task(process_flex_queue())

# Flask와 디스코드 봇 실행
if __name__ == "__main__":
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))),
        daemon=True
    )
    flask_thread.start()
    bot.run(DISCORD_TOKEN)
