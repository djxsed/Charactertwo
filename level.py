import discord
from discord.ext import commands
import aiosqlite
import asyncio
from discord.ext.commands import CooldownMapping, BucketType

# 봇 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
cooldown = CooldownMapping.from_cooldown(1, 5.0, BucketType.user)  # 5초 쿨다운

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# 데이터베이스 초기화
async def init_db():
    async with aiosqlite.connect('users.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1
            )
        ''')
        await db.commit()

# 경험치와 레벨 계산
def get_level_xp(level):
    return level * 100  # 레벨당 필요한 경험치

async def add_xp(user_id, guild_id, xp, channel=None):
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT xp, level FROM users WHERE user_id = ? AND guild_id = ?', (user_id, guild_id))
        row = await cursor.fetchone()
        
        if row is None:
            await db.execute('INSERT INTO users (user_id, guild_id, xp, level) VALUES (?, ?, ?, 1)', (user_id, guild_id, xp))
            await db.commit()
            return 1, xp
        
        current_xp, current_level = row
        new_xp = current_xp + xp
        new_level = current_level
        
        # 레벨업 확인
        while new_xp >= get_level_xp(new_level) and new_level < 30:
            new_xp -= get_level_xp(new_level)
            new_level += 1
            # 레벨업 채널에 알림
            if channel and new_level > current_level:
                levelup_channel = discord.utils.get(channel.guild.channels, name="레벨업")
                if levelup_channel:
                    user = channel.guild.get_member(user_id)
                    await levelup_channel.send(f'{user.mention}님이 레벨 {new_level}로 올라갔어요!')
        
        # 경험치가 0 미만으로 떨어지지 않도록
        new_xp = max(0, new_xp)
        
        await db.execute('UPDATE users SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?', (new_xp, new_level, user_id, guild_id))
        await db.commit()
        
        return new_level, new_xp

# 봇 시작 시
@bot.event
async def on_ready():
    print(f'{bot.user}가 온라인입니다!')
    await init_db()

# 메시지 처리
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    # 쿨다운 체크
    bucket = cooldown.get_bucket(message)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        return
    
    # 글자 수로 경험치 계산 (공백 포함)
    xp = len(message.content)
    if xp > 0:
        await add_xp(message.author.id, message.guild.id, xp, message.channel)
    
    await bot.process_commands(message)

# 레벨 확인 명령어
@bot.command(name="레벨")
async def level(ctx, member: discord.Member = None):
    member = member or ctx.author
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT xp, level FROM users WHERE user_id = ? AND guild_id = ?', (member.id, ctx.guild.id))
        row = await cursor.fetchone()
        
        if row is None:
            await ctx.send(f'{member.display_name}님은 아직 경험치가 없어요!')
        else:
            xp, level = row
            await ctx.send(f'{member.display_name}님은 현재 레벨 {level}이고, 경험치는 {xp}/{get_level_xp(level)}이에요!')

# 리더보드 명령어
@bot.command(name="리더보드")
async def leaderboard(ctx):
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT user_id, xp, level FROM users WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 5', (ctx.guild.id,))
        rows = await cursor.fetchall()
        
        if not rows:
            await ctx.send('아직 리더보드에 데이터가 없어요!')
            return
        
        embed = discord.Embed(title=f"{ctx.guild.name} 리더보드", color=discord.Color.blue())
        for i, (user_id, xp, level) in enumerate(rows, 1):
            user = ctx.guild.get_member(user_id)
            if user:
                embed.add_field(name=f"{i}. {user.display_name}", value=f"레벨 {level} | XP: {xp}/{get_level_xp(level)}", inline=False)
        
        await ctx.send(embed=embed)

# 경험치 추가 명령어 (관리실 전용)
@bot.command(name="경험치추가")
async def add_xp_command(ctx, member: discord.Member, xp: int):
    if ctx.channel.name != "관리실":
        await ctx.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!")
        return
    
    if xp <= 0:
        await ctx.send("추가할 경험치는 양수여야 합니다!")
        return
        
    new_level, new_xp = await add_xp(member.id, ctx.guild.id, xp, ctx.channel)
    await ctx.send(f'{member.display_name}님에게 {xp}만큼의 경험치를 추가했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# 경험치 제거 명령어 (관리실 전용)
@bot.command(name="경험치제거")
async def remove_xp_command(ctx, member: discord.Member, xp: int):
    if ctx.channel.name != "관리실":
        await ctx.send("이 명령어는 관리실 채널에서만 사용할 수 있습니다!")
        return
    
    if xp <= 0:
        await ctx.send("제거할 경험치는 양수여야 합니다!")
        return
        
    new_level, new_xp = await add_xp(member.id, ctx.guild.id, -xp, ctx.channel)
    await ctx.send(f'{member.display_name}님에게서 {xp}만큼의 경험치를 제거했습니다! 현재 레벨: {new_level}, 경험치: {new_xp}/{get_level_xp(new_level)}')

# 봇 실행
import os
bot.run(DISCORD_TOKEN)
