
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

        pool = await asyncpg.create_pool(normalized_url)
        async with pool.acquire() as conn:
            logger.info("데이터베이스 테이블 생성 중...")
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
        logger.error(f"데이터베이스 초기화 오류: {e}")
        raise

# 경험치와 레벨 계산
def get_level_xp(level):
    return level * 200

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
                    user_id, guild_id, xp
                )
                return 1, xp

            current_xp, current_level = row['xp'], row['level']
            new_xp = current_xp + xp
            new_level = current_level

            # 레벨업 로직
            level_up_occurred = False
            while new_xp >= get_level_xp(new_level) and new_level < 30:
                new_xp -= get_level_xp(new_level)
                new_level += 1
                level_up_occurred = True
            
            # 경험치가 음수가 되지 않도록
            new_xp = max(0, new_xp)

            # 데이터베이스 업데이트
            await conn.execute(
                'UPDATE users SET xp = $1, level = $2 WHERE user_id = $3 AND guild_id = $4',
                new_xp, new_level, user_id, guild_id
            )
            
            # 레벨업 알림 처리를 별도로 진행
            if level_up_occurred and channel and new_level > current_level:
                try:
                    guild = channel.guild
                    levelup_channel = discord.utils.get(guild.channels, name="레벨업")
                    if levelup_channel:
                        user = guild.get_member(user_id)
                        if user:
                            await levelup_channel.send(f'{user.mention}님이 레벨 {new_level}로 올라갔어요!')
                            try:
                                await user.edit(nick=f"[{new_level}렙] {user.name}")
                            except discord.errors.Forbidden:
                                logger.warning(f"닉네임 변경 권한이 없습니다: {user.id}")
                except Exception as e:
                    logger.error(f"레벨업 알림 처리 중 오류 발생: {e}")
                    # 레벨업 알림에서 오류가 발생해도 경험치 처리는 계속 진행
            
            return new_level, new_xp
    except Exception as e:
        logger.error(f"경험치 추가 중 오류 발생: {e}")
        return 1, 0

# 메시지 처리
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    xp = len(message.content)
    if xp > 0:
        if hasattr(bot, 'db_pool') and bot.db_pool is not None:
            try:
                await add_xp(message.author.id, message.guild.id, xp, message.channel, bot.db_pool)
            except Exception as e:
                logger.error(f"메시지 처리 중 경험치 추가 오류: {e}")
        else:
            logger.warning("db_pool이 아직 준비되지 않았습니다. 다시 시도해주세요.")

    await bot.process_commands(message)

# 명령어 동기화 함수
async def sync_commands():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"명령어 동기화 시도 {attempt}/{max_retries}...")
            # 명령어를 명시적으로 등록
            bot.tree.clear_commands(guild=None)  # 기존 명령어 초기화
            bot.tree.add_command(level)
            bot.tree.add_command(leaderboard)
            bot.tree.add_command(add_xp_command)
            bot.tree.add_command(remove_xp_command)
            # 글로벌 명령어 동기화
            synced = await bot.tree.sync()
            logger.info(f"명령어가 동기화되었어: {len(synced)}개의 명령어 등록됨")
            return synced
        except Exception as e:
            logger.error(f"명령어 동기화 실패 (시도 {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(5)  # 재시도 전 대기
            else:
                logger.error("최대 재시도 횟수 초과. 명령어 동기화 실패.")
                raise

# 레벨 확인 명령어
@app_commands.command(name="레벨", description="현재 레벨과 경험치를 확인해!")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def level(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    member = member or interaction.user
    try:
        async with bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT xp, level FROM users WHERE user_id = $1 AND guild_id = $2',
                member.id, interaction.guild.id
            )

            if row is None:
                await interaction.followup.send(f'{member.display_name}님은 아직 경험치가 없어요!')
            else:
                xp, level = row['xp'], row['level']
                await interaction.followup.send(f'{member.display_name}님은 현재 레벨 {level}이고, 경험치는 {xp}/{get_level_xp(level)}이에요!')
    except Exception as e:
        logger.error(f"레벨 명령어 실행 중 오류 발생: {e}")
        await interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.")

# 리더보드 명령어
@app_commands.command(name="리더보드", description="서버의 상위 5명 레벨 랭킹을 확인해!")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, xp, level FROM users WHERE guild_id = $1 ORDER BY level DESC, xp DESC LIMIT 5',
                interaction.guild.id
            )

            if not rows:
                await interaction.followup.send('아직 리더보드에 데이터가 없어요!')
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

            await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"리더보드 명령어 실행 중 오류 발생: {e}")
        await interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.")

# 경험치 추가 명령어 (관리자 전용)
@app_commands.command(name="경험치추가", description="관리실에서 경험치를 추가해! (관리자 전용)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def add_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    await interaction.response.defer()
    try:
        if interaction.channel.name != "관리실":
            await interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
            return

        if xp <= 0:
            await interaction.followup.send("추가할 경험치는 양수여야 합니다!", ephemeral=True)
            return

        # 경험치 추가 처리
        new_level, new_xp = await add_xp(member.id, interaction.guild.id, xp, None, bot.db_pool)
        
        # 성공 메시지 전송
        await interaction.followup.send(f'{member.display_name}님에게 {xp}만큼의 경험치를 추가했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')
        
        # 별도로 닉네임 업데이트 시도
        try:
            await member.edit(nick=f"[{new_level}렙] {member.name}")
        except discord.errors.Forbidden:
            await interaction.followup.send("봇에게 해당 유저의 닉네임을 변경할 권한이 없습니다.", ephemeral=True)
        
        # 레벨업 채널에 알림 전송
        levelup_channel = discord.utils.get(interaction.guild.channels, name="레벨업")
        if levelup_channel:
            await levelup_channel.send(f'{member.mention}님의 경험치가 {xp} 추가되었습니다. 현재 레벨: {new_level}')
    except Exception as e:
        logger.error(f"경험치 추가 명령어 실행 중 오류 발생: {e}")
        await interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.")

# 경험치 제거 명령어 (관리자 전용)
@app_commands.command(name="경험치제거", description="관리실에서 경험치를 제거해! (관리자 전용)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def remove_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    await interaction.response.defer()
    try:
        if interaction.channel.name != "관리실":
            await interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
            return

        if xp <= 0:
            await interaction.followup.send("제거할 경험치는 양수여야 합니다!", ephemeral=True)
            return

        # 경험치 제거 처리 (channel=None으로 레벨업 알림을 분리)
        new_level, new_xp = await add_xp(member.id, interaction.guild.id, -xp, None, bot.db_pool)
        
        # 성공 메시지 전송
        await interaction.followup.send(f'{member.display_name}님에게서 {xp}만큼의 경험치를 제거했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')
        
        # 별도로 닉네임 업데이트 시도
        try:
            await member.edit(nick=f"[{new_level}렙] {member.name}")
        except discord.errors.Forbidden:
            await interaction.followup.send("봇에게 해당 유저의 닉네임을 변경할 권한이 없습니다.", ephemeral=True)
    except Exception as e:
        logger.error(f"경험치 제거 명령어 실행 중 오류 발생: {e}")
        await interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.")

# 쿨다운 에러 처리
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"{error.retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
    else:
        logger.error(f"명령어 실행 중 오류 발생: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True)
        else:
            await interaction.followup.send("명령어 실행 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True)

# 봇 시작 시 실행
@bot.event
async def on_ready():
    logger.info(f'봇이 로그인했어: {bot.user}')
    try:
        bot.db_pool = await init_db()
        await sync_commands()
    except Exception as e:
        logger.error(f"봇 초기화 중 오류 발생: {e}")

# 봇과 웹 서버를 동시에 실행
async def main():
    try:
        await start_web_server()
        await bot.start(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"봇 또는 웹 서버 실행 중 오류 발생: {e}")
        raise

if __name__ == "__main__":
    try:
        logger.info("봇과 웹 서버를 시작합니다...")
        asyncio.run(main())
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류 발생: {e}")
        raise
