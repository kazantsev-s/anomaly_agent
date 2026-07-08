import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from config import get_settings
from db.postgres import init_db, ping_db
from logger import init_logger
from agent.graph import agent_graph

dp = Dispatcher()
logger = init_logger()


@dp.message(Command('start'))
async def start(message):
    await message.answer('Агент по поиску аномалий. Проверьте базу с помощью команды /health.')


@dp.message(Command('health'))
async def health(message):
    try:
        await ping_db()
    except Exception:
        logger.exception('Ошибка подключения к БД')
        await message.answer('Ошибка подключения к БД')
        return

    await message.answer('Успешно подключено к БД')


@dp.message(Command('ask'))
async def ask_agent(message):
    user_prompt = message.text.replace('/ask', '').strip()

    if user_prompt is None or user_prompt == '':
        logger.warning('Пользователь не ввел запрос после /ask')
        await message.answer('Введите ваш запрос после /ask')
        return
    
    logger.info(f'Пользователь ввел запрос: {user_prompt}')

    try:
        # https://reference.langchain.com/python/langgraph/pregel/remote/RemoteGraph/ainvoke
        logger.info(f'Вызов AI-агента')
        agent_result = await agent_graph.ainvoke({
            'prompt': user_prompt,
            'sql_query': '',
            'sql_result': '',
            'answer': '',
        })
    except Exception:
        logger.exception('Ошибка вызова AI-агента')
        await message.answer('Не удалось получить ответ от AI-агента')
        return

    logger.info(f'AI-агент вернул ответ')
    await message.answer(agent_result['answer'])


async def main():
    settings = get_settings()

    await init_db()

    try:
        bot = Bot(token=settings.tg_token)
        await dp.start_polling(bot)
    except Exception:
        logger.exception('Ошибка инициализации бота')


if __name__ == '__main__':
    asyncio.run(main())
