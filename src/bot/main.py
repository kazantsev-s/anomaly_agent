import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from config import get_settings
from db.postgres import init_db, ping_db
from logger import init_logger

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
