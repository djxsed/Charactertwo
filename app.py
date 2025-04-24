import os
import json
import time
import discord
from discord.ext import commands
from openai import OpenAI
import aiosqlite
from datetime import datetime, timezone  # UTC 시간 처리를 위해 필요
from dotenv import load_dotenv
import asyncio
import hashlib
import logging

# 로그 설정: 마치 일기 쓰듯이 프로그램이 뭘 했는지 기록해
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("batch_processor.log"),  # 로그를 파일에 저장
        logging.StreamHandler()  # 터미널에도 출력
    ]
)
logger = logging.getLogger(__name__)

# 환경 변수 불러오기: 비밀 정보(예: 비밀번호)를 안전하게 저장해둔 곳에서 가져와
load_dotenv()
DISCORD_TOKEN = ..
OPENAI_API_KEY = ..

# OpenAI 클라이언트 초기화: OpenAI와 대화할 준비를 해
try:
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY가 .env 파일에 없어!")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI 클라이언트 초기화 성공")
except Exception as e:
    logger.error(f"OpenAI 클라이언트 초기화 실패: {str(e)}")
    raise

# 디스코드 봇 설정: 디스코드에서 메시지를 보내고 받을 준비를 해
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# 로그 채널 ID: 오류나 결과를 기록할 디스코드 채널
LOG_CHANNEL_ID = 1358060156742533231  # 너의 서버 로그 채널 ID로 바꿔!

# 허용된 역할: 캐릭터가 가질 수 있는 역할 목록
ALLOWED_ROLES = ["학생", "선생님", "A.M.L"]

async def get_pending_tasks():
    """대기 중인 작업을 데이터베이스에서 가져와. 마치 할 일 목록을 확인하는 거야!"""
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
    """작업 상태를 업데이트해. 예: '대기 중' -> '처리 중' -> '완료'"""
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
    """캐릭터 심사 결과를 데이터베이스에 저장해. 마치 시험 결과를 기록하는 거야!"""
    try:
        description_hash = hashlib.md5(description.encode()).hexdigest()
        timestamp = datetime.now(timezone.UTC).isoformat()
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
    """디스코드에 메시지를 보내. 마치 친구한테 문자 보내는 것 같아!"""
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
    """작업을 OpenAI Batch API용 파일로 만들어. 마치 편지 봉투에 내용물을 넣는 거야!"""
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
    """작업을 처리하는 메인 함수야. 마치 공장에서 물건을 만드는 기계 같아!"""
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

                        if pass_status:
                            for role in ALLOWED_ROLES:
                                if f"역할: {role}" in response:
                                    role_name = role
                                    break
                            if not role_name:
                                await save_character_result(character_id, description, False, "유효한 역할 없음", None)
                                message = "❌ 앗, 유효한 역할이 없네! 학생, 선생님, A.M.L 중 하나로 설정해줘~ 😊"
                            else:
                                await save_character_result(character_id, description, True, "통과", role_name)
                                message = f"🎉 우와, 대단해! 통과했어~ 역할: {role_name} 🎊"
                        else:
                            await save_character_result(character_id, description, False, reason, None)
                            message = f"❌ 아쉽게도... {reason} 다시 수정해서 도전해봐! 내가 응원할게~ 💪"

                        if pass_status and role_name:
                            try:
                                # 서버 ID는 채널 ID에서 추정
                                guild_id = int(channel_id.split("-")[0]) if "-" in channel_id else int(channel_id)
                                guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
                                if guild:
                                    member = await guild.fetch_member(int(user_id))
                                    role = discord.utils.get(guild.roles, name=role_name)
                                    if role:
                                        await member.add_roles(role)
                                        message += f" (역할 `{role_name}`도 멋지게 부여했어! 😎)"
                                    else:
                                        message += f" (역할 `{role_name}`이 서버에 없네... 관리자한테 물어볼까? 🤔)"
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
    """봇이 디스코드에 연결되면 실행돼"""
    logger.info(f'Batch 처리자 봇 로그인됨: {bot.user}')
    try:
        await process_batch()
    except Exception as e:
        logger.error(f"Batch 처리 시작 실패: {str(e)}")
        raise

if __name__ == "__main__":
    """프로그램을 시작하는 부분이야"""
    logger.info("Batch Processor 스크립트 시작")
    try:
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN이 .env 파일에 없어!")
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"디스코드 봇 실행 실패: {str(e)}")
        raise
