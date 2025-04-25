import os
import json
import time
import discord
from discord.ext import commands
from openai import OpenAI
import aiosqlite
from datetime import datetime, timezone
from dotenv import load_dotenv
import asyncio
import hashlib
import logging

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("batch_processor.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 환경 변수 불러오기
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI 클라이언트 초기화
try:
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 .env 파일에 없어!")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI 클라이언트 초기화 성공")
except Exception as e:
    logger.error(f"OpenAI 클라이언트 초기화 실패: {str(e)}")
    raise

# 디스코드 봇 설정
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# 로그 채널 ID
LOG_CHANNEL_ID = 1358060156742533231

# 기본 설정값
DEFAULT_ALLOWED_RACES = ["인간", "마법사", "A.M.L", "요괴"]
DEFAULT_ALLOWED_ROLES = ["학생", "선생님", "A.M.L"]
DEFAULT_CHECK_CHANNEL_NAME = "입학-신청서"

async def get_settings(guild_id):
    """서버별 설정 조회"""
    try:
        async with aiosqlite.connect("characters.db") as db:
            async with db.execute("SELECT allowed_roles, check_channel_name FROM settings WHERE guild_id = ?", (str(guild_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    allowed_roles = row[0].split(",") if row[0] else DEFAULT_ALLOWED_ROLES
                    check_channel_name = row[1] if row[1] else DEFAULT_CHECK_CHANNEL_NAME
                    return allowed_roles, check_channel_name
                return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME
    except Exception as e:
        logger.error(f"설정 조회 실패: guild_id={guild_id}, error={str(e)}")
        return DEFAULT_ALLOWED_ROLES, DEFAULT_CHECK_CHANNEL_NAME

async def get_pending_tasks():
    """대기 중인 작업을 데이터베이스에서 가져오기"""
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

async def update_task_status(task_id: str, status: str, result: dict = None):
    """작업 상태 업데이트"""
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

async def save_character_result(character_id: str, description: str, pass_status: bool, reason: str, role_name: str):
    """캐릭터 심사 결과 저장"""
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

async def send_discord_message(channel_id: str, thread_id: str, user_id: str, message: str):
    """디스코드에 메시지 보내기"""
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

def create_jsonl_file(tasks: list, filename: str):
    """OpenAI Batch API용 .jsonl 파일 생성"""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            for task in tasks:
                task_id, _, _, _, _, _, task_type, prompt = task
                request = {
                    "custom_id": task_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": "gpt-4.1-mini",
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
    """Batch 작업 처리 메인 함수"""
    logger.info("Batch 처리 시작")
    while True:
        try:
            # 대기 중인 작업 가져오기
            tasks = await get_pending_tasks()
            if not tasks:
                logger.info("대기 중인 작업이 없습니다. 30초 대기...")
                await asyncio.sleep(30)
                continue

            # .jsonl 파일 생성
            jsonl_filename = f"batch_{int(time.time())}.jsonl"
            create_jsonl_file(tasks, jsonl_filename)

            try:
                # OpenAI Batch API에 파일 업로드
                with open(jsonl_filename, "rb") as f:
                    file_response = openai_client.files.create(file=f, purpose="batch")
                file_id = file_response.id
                logger.info(f"파일 업로드 성공: file_id={file_id}")

                # Batch 작업 생성
                batch_response = openai_client.batches.create(
                    input_file_id=file_id,
                    endpoint="/v1/chat/completions",
                    completion_window="24h",
                    metadata={"description": "Character review batch"}
                )
                batch_id = batch_response.id
                logger.info(f"Batch 작업 생성: batch_id={batch_id}")

                # 작업 상태를 'processing'으로 업데이트
                for task in tasks:
                    task_id = task[0]
                    await update_task_status(task_id, "processing")

                # Batch 작업 상태 확인
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

                # 결과 가져오기
                output_file_id = batch_status.output_file_id
                output_content = openai_client.files.content(output_file_id).text
                results = [json.loads(line) for line in output_content.splitlines()]
                logger.info(f"Batch 결과 가져옴: {len(results)}개 작업")

                # 결과 처리
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
                        else:
                            await send_discord_message(
                                channel_id, thread_id, user_id,
                                f"❌ 앗, 피드백 처리 중 오류: {error_message} 😓"
                            )
                        continue

                    response = result["response"]["body"]["choices"][0]["message"]["content"]
                    await update_task_status(task_id, "completed", {"response": response})
                    logger.info(f"작업 완료: task_id={task_id}, response={response}")

                    if task_type == "character_check":
                        pass_status = "✅" in response
                        role_name = None
                        reason = response.replace("✅", "").replace("❌", "").strip()

                        # 서버별 허용된 역할 조회
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

                                    # 역할 확인
                                    has_role = False
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
                                        message = "🎉 이미 통과된 캐릭터야~ 역할은 이미 있어! 🎊"
                                    else:
                                        # 역할 부여
                                        if role:
                                            try:
                                                await member.add_roles(role)
                                                message += f" (역할 `{role_name}` 부여했어! 😊)"
                                            except discord.Forbidden:
                                                message += f" (역할 `{role_name}` 부여 실패... 권한이 없나 봐! 🥺)"
                                        else:
                                            message += f" (역할 `{role_name}`이 서버에 없어... 관리자한테 물어봐! 🤔)"

                                        # 종족 역할 부여
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
                    else:  # 피드백 처리
                        if "너무 높습니다" in response:
                            response = response.replace("너무 높습니다", "너무 쎄서 내가 깜짝 놀랐잖아! 😲 조금만 낮춰줄래?")
                        elif "규칙에 맞지 않습니다" in response:
                            response = response.replace("규칙에 맞지 않습니다", "규칙이랑 안 맞네~ 🤔 다시 한 번 체크해볼까?")
                        await send_discord_message(channel_id, thread_id, user_id, f"💬 {response}")

                # 로그 채널에 완료 기록
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
                # .jsonl 파일 삭제
                if os.path.exists(jsonl_filename):
                    try:
                        os.remove(jsonl_filename)
                        logger.info(f".jsonl 파일 삭제: {jsonl_filename}")
                    except Exception as e:
                        logger.error(f".jsonl 파일 삭제 실패: {str(e)}")

        except Exception as e:
            logger.error(f"Batch 처리 루프 오류: {str(e)}")
            await asyncio.sleep(60)

@bot.event
async def on_ready():
    """봇이 디스코드에 연결되면 실행"""
    logger.info(f'Batch 처리자 봇 로그인됨: {bot.user}')
    try:
        await process_batch()
    except Exception as e:
        logger.error(f"Batch 처리 시작 실패: {str(e)}")
        raise

if __name__ == "__main__":
    """프로그램 시작"""
    logger.info("Batch Processor 스크립트 시작")
    try:
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN이 .env 파일에 없어!")
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"디스코드 봇 실행 실패: {str(e)}")
        raise
