import asyncio
from html import unescape
import re
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from config import get_settings
from db.postgres import init_db, ping_db
from logger import init_logger
from agent.analyze_graph import analyze_graph
from agent.ask_graph import ask_graph
from aiogram.enums import ParseMode

dp = Dispatcher()
logger = init_logger()


def prepare_telegram_html(text: str) -> str:
    # Telegram HTML не поддерживает br, модель иногда все равно его добавляет
    return re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)


def strip_html_tags(text: str) -> str:
    # Fallback для неожиданных HTML-тегов, чтобы отчет не терялся целиком
    text = prepare_telegram_html(text)
    text = re.sub(r'</(p|div|li)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return unescape(text)


async def answer_html(message, text: str):
    try:
        await message.answer(prepare_telegram_html(text), parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        logger.exception('Telegram не смог разобрать HTML, отправляем текст без тегов')
        await message.answer(strip_html_tags(text))


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
        logger.info(f'Вызов агента')
        agent_result = await ask_graph.ainvoke({
            'prompt': user_prompt,
        })
    except Exception:
        logger.exception('Ошибка вызова агента')
        await message.answer('Не удалось получить ответ от агента')
        return

    logger.info(f'Агент вернул ответ')
    await answer_html(message, agent_result['answer'])


@dp.message(Command('analyze'))
async def analyze_table(message):
    settings = get_settings()
    # Если пользователь не указал имя таблицы, используем таблицу по умолчанию
    table_name = message.text.replace('/analyze', '').strip() or settings.analyze_default_table

    try:
        logger.info(f'Вызов анализа аномалий для таблицы {table_name}')
        answer_sent = False

        async for graph_update in analyze_graph.astream({'table_name': table_name}, stream_mode='updates'):
            if 'final_anomaly_answer' in graph_update:
                agent_result = graph_update['final_anomaly_answer']
                await answer_html(message, agent_result['answer'])
                answer_sent = True

                if agent_result.get('test_id'):
                    await message.answer('Рассчитываю качество работы агента...')

            if 'evaluate_analysis' in graph_update:
                agent_result = graph_update['evaluate_analysis']
                evaluation_answer = agent_result.get('evaluation_answer')
                if evaluation_answer:
                    await answer_html(message, evaluation_answer)

        if not answer_sent:
            logger.error('Граф анализа завершился без финального ответа')
            await message.answer('Не удалось получить итоговый отчет')
    except Exception:
        logger.exception('Ошибка анализа аномалий')
        await message.answer('Не удалось выполнить анализ аномалий')
        return


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
