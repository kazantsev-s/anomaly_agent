import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from config import get_settings
from db.postgres import init_db, ping_db
from logger import init_logger
from agent.analyze_graph import analyze_graph
from agent.ask_graph import ask_graph
from aiogram.enums import ParseMode

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
        agent_result = await ask_graph.ainvoke({
            'prompt': user_prompt,
        })
    except Exception:
        logger.exception('Ошибка вызова AI-агента')
        await message.answer('Не удалось получить ответ от AI-агента')
        return

    logger.info(f'AI-агент вернул ответ')
    await message.answer(agent_result['answer'], parse_mode=ParseMode.HTML)


@dp.message(Command('analyze'))
async def analyze_table(message):
    settings = get_settings()

    try:
        logger.info('Вызов анализа аномалий')
        agent_result = await analyze_graph.ainvoke({
            'table_name': settings.analyze_default_table,
        })
    except Exception:
        logger.exception('Ошибка анализа аномалий')
        await message.answer('Не удалось выполнить анализ аномалий')
        return

    await message.answer(agent_result['answer'], parse_mode=ParseMode.HTML)


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
