import discord
from discord.ext import commands
import asyncio
from discord.ext.commands import CooldownMapping, BucketType
import os
from dotenv import load_dotenv
import asyncpg
import urllib.parse
import re

# 환경 변수 불러오기
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

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
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")

        # DATABASE_URL 디버깅 출력 (비밀번호 마스킹)
        masked_url = DATABASE_URL
        if "://" in DATABASE_URL:
            scheme, rest = DATABASE_URL.split("://", 1)
            if "@" in rest:
                userinfo, hostinfo = rest.split("@", 1)
                if ":" in userinfo:
                    user, _ = userinfo.split(":", 1)
                    masked_url = f"{scheme}://{user}:[REDACTED]@{hostinfo}"
        print(f"Raw DATABASE_URL: {masked_url}")

        # 비밀번호에 특수 문자가 포함된 경우를 처리하기 위해 URL 정규화
        scheme_match = re.match(r"^(postgresql|postgres)://", DATABASE_URL, re.IGNORECASE)
        if not scheme_match:
            raise ValueError("DATABASE_URL은 'postgresql://' 또는 'postgres://'로 시작해야 합니다.")

        scheme = scheme_match.group(0)
        rest = DATABASE_URL[len(scheme):]
        userinfo, hostinfo = rest.split("@", 1)
        username, password = userinfo.split(":", 1) if ":" in userinfo else (userinfo, "")
        hostname_port, dbname = hostinfo.split("/", 1) if "/" in hostinfo else (hostinfo, "postgres")
        hostname, port = hostname_port.split(":", 1) if ":" in hostname_port else (hostname_port, "5432")

        encoded_password = urllib.parse.quote(password, safe='')
        normalized_url = f"postgresql://{username}:{encoded_password}@{hostname}:{port}/{dbname}"

        print(f"Normalized DATABASE_URL: {normalized_url}")

        pool = await asyncpg.create_pool(normalized_url)
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
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await interaction.response.send_message(f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()
    member = member or interaction.user
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

# 리더보드 명령어
@bot.tree.command(name="리더보드", description="서버의 상위 5명 레벨 랭킹을 확인해!")
async def leaderboard(interaction: discord.Interaction):
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await interaction.response.send_message(f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()
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

# 경험치 추가 명령어 (관리자 전용)
@bot.tree.command(name="경험치추가", description="관리실에서 경험치를 추가해! (관리자 전용)")
@commands.has_permissions(administrator=True)
async def add_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await interaction.response.send_message(f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()
    if interaction.channel.name != "관리실":
        await interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
        return
    
    if xp <= 0:
        await interaction.followup.send("추가할 경험치는 양수여야 합니다!", ephemeral=True)
        return
        
    new_level, new_xp = await add_xp(member.id, interaction.guild.id, xp, interaction.channel, bot.db_pool)
    await interaction.followup.send(f'{member.display_name}님에게 {xp}만큼의 경험치를 추가했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# 경험치 제거 명령어 (관리자 전용)
@bot.tree.command(name="경험치제거", description="관리실에서 경험치를 제거해! (관리자 전용)")
@commands.has_permissions(administrator=True)
async def remove_xp_command(interaction: discord.Interaction, member: discord.Member, xp: int):
    bucket = cooldown.get_bucket(interaction)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await interaction.response.send_message(f"{retry_after:.1f}초 후에 다시 시도해주세요!", ephemeral=True)
        return

    await interaction.response.defer()
    if interaction.channel.name != "관리실":
        await interaction.followup.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!", ephemeral=True)
        return
    
    if xp <= 0:
        await interaction.followup.send("제거할 경험치는 양수여야 합니다!", ephemeral=True)
        return
        
    new_level, new_xp = await add_xp(member.id, interaction.guild.id, -xp, interaction.channel, bot.db_pool)
    await interaction.followup.send(f'{member.display_name}님에게서 {xp}만큼의 경험치를 제거했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# 봇 시작 시 실행
@bot.event
async def on_ready():
    print(f'봇이 로그인했어: {bot.user}')
    bot.db_pool = await init_db()
    try:
        synced = await bot.tree.sync()
        print(f'명령어가 동기화되었어: {len(synced)}개의 명령어 등록됨')
    except Exception as e:
        print(f'명령어 동기화 실패: {e}')

# 봇 실행
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
