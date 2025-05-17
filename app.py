import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv
import asyncpg
import urllib.parse
import re
import logging
from aiohttp import web
import uuid
import time
import random
from collections import defaultdict

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 환경 변수 불러오기
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8000))

# 환경 변수 유효성 검사
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
    raise ValueError("DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
if not DATABASE_URL:
    logger.error("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
    raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")

# 봇 설정
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Rate Limit 관리
bot.rate_limit_until = 0  # 글로벌 rate limit 해제 시간
bot.xp_queue = defaultdict(list)  # 경험치 큐: (user_id, guild_id, xp, timestamp)

# aiohttp 웹 서버 설정
async def handle_root(request):
    return web.Response(text="Discord Bot is running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_root)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")

# 데이터베이스 초기화
async def init_db():
    try:
        logger.info("데이터베이스 초기화를 시작합니다...")
        scheme_match = re.match(r"^(postgresql|postgres)://", DATABASE_URL, re.IGNORECASE)
        if not scheme_match:
            logger.error("DATABASE_URL은 'postgresql://' 또는 'postgres://'로 시작해야 합니다.")
            raise ValueError("DATABASE_URL은 'postgresql://' 또는 'postgres://'로 시작해야 합니다.")

        scheme = scheme_match.group(0)
        rest = DATABASE_URL[len(scheme):]
        userinfo, hostinfo = rest.split("@", 1)
        username, password = userinfo.split(":", 1) if ":" in userinfo else (userinfo, "")
        hostname_port, dbname = hostinfo.split("/", 1) if "/" in hostinfo else (hostinfo, "postgres")
        hostname, port = hostname_port.split(":", 1) if ":" in hostname_port else (hostname_port, "5432")

        encoded_password = urllib.parse.quote(password, safe='')
        normalized_url = f"postgresql://{username}:{encoded_password}@{hostname}:{port}/{dbname}"

        logger.info(f"Normalized DATABASE_URL: {normalized_url}")

        pool = await asyncpg.create_pool(normalized_url, timeout=10)
        async with pool.acquire() as conn:
            logger.info("데이터베이스ibute('data:image/png;base64,...') # 이미지 삽입 (예시)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT,
                    guild_id BIGINT,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')
        logger.info("데이터베이스 초기화 완료.")
        return pool
    except Exception as e:
        logger.error(f"데이터베이스 초기화 오류: {e}", exc_info=True)
        raise

# 경험치와 레벨 계산
def get_level_xp(level):
    return level * 500

# API 호출에 재시도 로직 추가
async def with_retry(coro, max_retries=3, base_delay=1):
    for attempt in range(max_retries):
        if time.time() < bot.rate_limit_until:
            retry_after = bot.rate_limit_until - time.time()
            logger.warning(f"글로벌 rate limit 적용 중, {retry_after:.2f}초 후 재시도")
            await asyncio.sleep(retry_after)
            continue
        try:
            return await asyncio.wait_for(coro, timeout=10.0)
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limit error
                retry_after = float(e.response.headers.get('Retry-After', base_delay))
                if 'X-RateLimit-Scope' in e.response.headers and e.response.headers['X-RateLimit-Scope'] == 'global':
                    bot.rate_limit_until = time.time() + retry_after
                    logger.warning(f"글로벌 rate limit 발생, {retry_after:.2f}초 대기")
                else:
                    logger.warning(f"로컬 rate limit 발생, {retry_after:.2f}초 후 재시도 (시도 {attempt + 1}/{max_retries})")
                if retry_after > 600:  # 10분 이상 대기는 비정상, 최대 10분으로 제한
                    retry_after = 600
                    logger.warning("Retry-After가 비정상적으로 큼, 600초로 제한")
                await asyncio.sleep(retry_after + random.uniform(0.1, 0.5))
                if attempt == max_retries - 1:
                    raise
            else:
                logger.error(f"HTTP 예외 발생: {e}", exc_info=True)
                raise
        except asyncio.TimeoutError:
            logger.error(f"작업 타임아웃 (시도 {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                raise
        except Exception as e:
            logger.error(f"예외 발생: {e}", exc_info=True)
            raise

async def process_xp_queue():
    """주기적으로 경험치 큐를 처리"""
    while True:
        try:
            if bot.db_pool and bot.xp_queue:
                for (user_id, guild_id), entries in list(bot.xp_queue.items()):
                    total_xp = sum(xp for _, _, xp, _ in entries)
                    channel = bot.get_channel(entries[0][1])  # 첫 번째 메시지의 채널 사용
                    await add_xp(user_id, guild_id, total_xp, channel, bot.db_pool)
                    del bot.xp_queue[(user_id, guild_id)]
        except Exception as e:
            logger.error(f"경험치 큐 처리 중 오류: {e}", exc_info=True)
        await asyncio.sleep(10)  # 10초마다 큐 처리

async def add_xp(user_id, guild_id, xp, channel=None, pool=None):
    try:
        if pool is None:
            logger.error("데이터베이스 풀이 없습니다.")
            return 1, 0
        
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT xp, level FROM users WHERE user_id = $1 AND guild_id = $2',
                user_id, guild_id
            )

            if row is None:
                await conn.execute(
                    'INSERT INTO users (user_id, guild_id, xp, level) VALUES ($1, $2, $3, 1)',
                    user_id, guild_id, max(0, xp)
                )
                return 1, max(0, xp)

            current_xp, current_level = row['xp'], row['level']
            new_xp = current_xp + xp
            new_level = current_level

            # 레벨업/레벨다운 로직
            level_change_occurred = False
            while new_xp >= get_level_xp(new_level) and new_level < 30:
                new_xp -= get_level_xp(new_level)
                new_level += 1
                level_change_occurred = True
            while new_xp < 0 and new_level > 1:
                new_level -= 1
                new_xp += get_level_xp(new_level)
                level_change_occurred = True
            
            # 경험치가 음수가 되지 않도록
            new_xp = max(0, new_xp)

            # 데이터베이스 업데이트
            await conn.execute(
                'UPDATE users SET xp = $1, level = $2 WHERE user_id = $3 AND guild_id = $4',
                new_xp, new_level, user_id, guild_id
            )
            
            # 레벨 변경 알림 처리
            if level_change_occurred and channel:
                try:
                    guild = channel.guild
                    levelup_channel = discord.utils.get(guild.channels, name="레벨업")
                    if levelup_channel:
                        user = guild.get_member(user_id)
                        if user:
                            message = f'{user.mention}님이 레벨 {new_level}로 {"올라갔어요!" if xp > 0 else "내려갔어요!"}'
                            await with_retry(levelup_channel.send(message))
                            try:
                                await with_retry(user.edit(nick=f"[{new_level}렙] {user.name}"))
                            except discord.errors.Forbidden:
                                logger.warning(f"닉네임 변경 권한이 없습니다: {user.id}")
                except Exception as e:
                    logger.error(f"레벨 변경 알림 처리 중 오류 발생: {e}", exc_info=True)
            
            return new_level, new_xp
    except Exception as e:
        logger.error(f"경험치 처리 중 오류 발생: {e}", exc_info=True)
        return 1, 0

# 메시지 처리
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    xp = len(message.content)
    if xp > 0:
        if hasattr(bot, 'db_pool') and bot.db_pool is not None:
            # 경험치를 큐에 추가
            bot.xp_queue[(message.author.id, message.guild.id)].append(
                (message.author.id, message.channel.id, xp, time.time())
            )
        else:
            logger.warning("db_pool이 아직 준비되지 않았습니다.")

    await bot.process_commands(message)

# 명령어 동기화 함수
async def sync_commands():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"명령어 동기화 시도 {attempt}/{max_retries}...")
            bot.tree.clear_commands(guild=None)
            bot.tree.add_command(level)
            bot.tree.add_command(leaderboard)
            bot.tree.add_command(add_xp_command)
            bot.tree.add_command(remove_xp_command)
            synced = await with_retry(bot.tree.sync())
            logger.info(f"명령어가 동기화되었어: {len(synced)}개의 명령어 등록됨")
            return synced
        except Exception as e:
            logger.error(f"명령어 동기화 실패 (시도 {attempt}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries:
                await asyncio.sleep(5)
            else:
                logger.error("최대 재시도 횟수 초과. 명령어 동기화 실패.")
                raise

# 레벨 확인 명령어
@app_commands.command(name="레벨", description="현재 레벨과 경험치를 확인해!")
@app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))
async def level(interaction: discord.Interaction, member: discord.Member = None):
    try:
        if time.time() < bot.rate_limit_until:
            await interaction.response.send_message(
                f"현재 API rate limit에 걸려 있습니다. 약 {(bot.rate_limit_until - time.time())/60:.1f}분 후 다시 시도해주세요.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        member = member or interaction.user
        async with bot.db_pool.acquire() as conn:
            row = await asyncio.wait_for(
                conn.fetchrow(
                    'SELECT xp, level FROM users WHERE user_id = $1 AND guild_id = $2',
                    member.id, interaction.guild.id
                ),
                timeout=5.0
            )

            message = f'{member.display_name}님은 아직 경험치가 없어요!' if row is None else \
                     f'{member.display_name}님은 현재 레벨 {row["level"]}이고, 경험치는 {row["xp"]}/{get_level_xp(row["level"])}이에요!'
            await with_retry(interaction.followup.send(message, ephemeral=True))
    except asyncio.TimeoutError:
        logger.error(f"레벨 명령어 데이터베이스 쿼리 타임아웃: user_id={member.id}, guild_id={interaction.guild.id}")
        await with_retry(interaction.followup.send("데이터베이스 응답이 느립니다. 나중에 다시 시도해주세요.", ephemeral=True))
    except Exception as e:
        logger.error(f"레벨 명령어 실행 중 오류 발생: {e}", exc_info=True)
        await with_retry(interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True))

# 리더보드 명령어
@app_commands.command(name="리더보드", description="서버의 상위 5명 레벨 랭킹을 확인해!")
@app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))
async def leaderboard(interaction: discord.Interaction):
    try:
        if time.time() < bot.rate_limit_until:
            await interaction.response.send_message(
                f"현재 API rate limit에 걸려 있습니다. 약 {(bot.rate_limit_until - time.time())/60:.1f}분 후 다시 시도해주세요.",
                ephemeral=True
            )
            return
        await interaction.response.defer()
        async with bot.db_pool.acquire() as conn:
            rows = await asyncio.wait_for(
                conn.fetch(
                    'SELECT user_id, xp, level FROM users WHERE guild_id = $1 ORDER BY level DESC, xp DESC LIMIT 5',
                    interaction.guild.id
                ),
                timeout=5.0
            )

            if not rows:
                await with_retry(interaction.followup.send('아직 리더보드에 데이터가 없어요!'))
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

            await with_retry(interaction.followup.send(embed=embed))
    except asyncio.TimeoutError:
        logger.error(f"리더보드 명령어 데이터베이스 쿼리 타임아웃: guild_id={interaction.guild.id}")
        await with_retry(interaction.followup.send("데이터베이스 응답이 느립니다. 나중에 다시 시도해주세요."))
    except Exception as e:
        logger.error(f"리더보드 명령어 실행 중 오류 발생: {e}", exc_info=True)
        await with_retry(interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요."))

# 경험치 추가 명령어 (관리자 전용)
@app_commands.command(name="경험치추가", description="관리실에서 경험치를 추가해! (관리자 전용)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))
async def add_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    try:
        if time.time() < bot.rate_limit_until:
            await interaction.response.send_message(
                f"현재 API rate limit에 걸려 있습니다. 약 {(bot.rate_limit_until - time.time())/60:.1f}분 후 다시 시도해주세요.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if interaction.channel.name != "관리실":
            await with_retry(interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True))
            return

        if xp <= 0:
            await with_retry(interaction.followup.send("추가할 경험치는 양수여야 합니다!", ephemeral=True))
            return

        new_level, new_xp = await add_xp(member.id, interaction.guild.id, xp, interaction.channel, bot.db_pool)
        
        await with_retry(interaction.followup.send(
            f'{member.display_name}님에게 {xp}만큼의 경험치를 추가했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}',
            ephemeral=True
        ))
        
        try:
            await with_retry(member.edit(nick=f"[{new_level}렙] {member.name}"))
        except discord.errors.Forbidden:
            await with_retry(interaction.followup.send("봇에게 해당 유저의 닉네임을 변경할 권한이 없습니다.", ephemeral=True))
        
    except Exception as e:
        logger.error(f"경험치 추가 명령어 실행 중 오류 발생: {e}", exc_info=True)
        await with_retry(interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True))

# 경험치 제거 명령어 (관리자 전용)
@app_commands.command(name="경험치제거", description="관리실에서 경험치를 제거해! (관리자 전용)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))
async def remove_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    try:
        if time.time() < bot.rate_limit_until:
            await interaction.response.send_message(
                f"현재 API rate limit에 걸려 있습니다. 약 {(bot.rate_limit_until - time.time())/60:.1f}분 후 다시 시도해주세요.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if interaction.channel.name != "관리실":
            await with_retry(interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True))
            return

        if xp <= 0:
            await with_retry(interaction.followup.send("제거할 경험치는 양수여야 합니다!", ephemeral=True))
            return

        new_level, new_xp = await add_xp(member.id, interaction.guild.id, -xp, interaction.channel, bot.db_pool)
        
        await with_retry(interaction.followup.send(
            f'{member.display_name}님에게서 {xp}만큼의 경험치를 제거했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}',
            ephemeral=True
        ))
        
        try:
            await with_retry(member.edit(nick=f"[{new_level}렙] {member.name}"))
        except discord.errors.Forbidden:
            await with_retry(interaction.followup.send("봇에게 해당 유저의 닉네임을 변경할 권한이 없습니다.", ephemeral=True))
        
    except Exception as e:
        logger.error(f"경험치 제거 명령어 실행 중 오류 발생: {e}", exc_info=True)
        await with_retry(interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True))

# 쿨다운 및 에러 처리
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if isinstance(error, app_commands.CommandOnCooldown):
            await with_retry(interaction.response.send_message(
                f"{error.retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True
            ))
        else:
            logger.error(f"명령어 실행 중 오류 발생: {error}", exc_info=True)
            message = "명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요."
            if not interaction.response.is_done():
                await with_retry(interaction.response.send_message(message, ephemeral=True))
            else:
                await with_retry(interaction.followup.send(message, ephemeral=True))
    except Exception as e:
        logger.error(f"에러 핸들러에서 추가 오류 발생: {e}", exc_info=True)

# 봇 시작 시 실행
@bot.event
async def on_ready():
    logger.info(f'봇이 로그인했어: {bot.user}')
    try:
        bot.db_pool = await init_db()
        await sync_commands()
        bot.loop.create_task(process_xp_queue())  # 경험치 큐 처리 시작
    except Exception as e:
        logger.error(f"봇 초기화 중 오류 발생: {e}", exc_info=True)
        raise

# 봇과 웹 서버를 동시에 실행
async def main():
    try:
        await start_web_server()
        await bot.start(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"봇 또는 웹 서버 실행 중 오류 발생: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        logger.info("봇과 웹 서버를 시작합니다...")
        asyncio.run(main())
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류 발생: {e}", exc_info=True)
        raise
