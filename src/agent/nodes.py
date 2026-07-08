from openai import AsyncOpenAI

from db.postgres import execute_sql_query
from agent.state import AgentState
from config import get_settings

import json

from logger import init_logger
logger = init_logger()

FIELDS_DESCRIPTION = '''
    Таблица: kolesa

    Поля:
    kolesa_id - уникальный id объявления со стороны сервиса
    kolesa_url - ссылка на объявление
    parsed_at - дата и время получения объявления
    brand - марка авто
    model - модель авто
    generation - поколение данного авто
    year - год выпуска авто
    city - город публикации объявления
    body_type - тип кузова авто
    fuel_type - тип топлива
    engine_volume - объем двигателя
    mileage - пробег в км
    transmission - тип коробки/трансмиссии
    drive_type - тип привода
    steering_wheel - расположение руля
    color - цвет авто
    kz_registration - растаможен ли в Казахстане
    imgs_count - количество фотографий в объявлении
    price - цена продажи
    img_filename - название файла с главным фото объявления
    img_url - ссылка на главное фото объявления
    found_img - нашли ли фото
'''

SQL_SYSTEM_PROMPT = f'''
    Ты SQL-агент для анализа объявлений о продаже авто.

    {FIELDS_DESCRIPTION}

    Напиши один PostgreSQL SELECT-запрос к таблице kolesa, чтобы получить данные для ответа на вопрос пользователя.

    Правила:
    - Верни только SQL-запрос без markdown, без ```sql и без объяснений.
    - Используй только SELECT.
    - Не используй INSERT, UPDATE, DELETE, DROP, ALTER, CREATE и другие изменяющие операции.
    - Не ставь точку с запятой в конце.
    - Если возвращаешь конкретные объявления, выбирай полезные поля: kolesa_id, brand, model, year, mileage, city, price, kolesa_url.
    - Для поиска аномалий используй агрегаты, сравнение со средними/медианами, группировки, сортировку.
    - Не делай SELECT *.
'''

ANSWER_SYSTEM_PROMPT = '''
    Ты агент для анализа объявлений о продаже авто.
    Отвечай на русском языке.

    Пользователь задал вопрос. Для ответа был выполнен SQL-запрос к таблице kolesa.
    Объясни результат понятно: что найдено, почему это может быть аномалией, на какие объявления или группы стоит обратить внимание.
    Если данных мало или запрос не дал строк, так и скажи.
'''

# Вспомогательные функции

def clean_sql_query(sql_query: str) -> str:
    query = sql_query.strip()

    if query.startswith('```sql'):
        query = query.removeprefix('```sql').strip()

    if query.startswith('```'):
        query = query.removeprefix('```').strip()

    if query.endswith('```'):
        query = query.removesuffix('```').strip()

    return query.rstrip(';').strip()


def get_openai_client() -> AsyncOpenAI:
    logger.info(f'Вызов ноды get_openai_client')
    settings = get_settings()

    if not settings.openai_api_key:
        logger.error('API ключ к OpenAI не задан в переменных окружения')
        raise ValueError('API ключ к OpenAI не задан в переменных окружения')

    # https://github.com/openai/openai-python#async-usage
    return AsyncOpenAI(api_key=settings.openai_api_key)


# Ноды графа агента

async def generate_sql_query(state: AgentState) -> AgentState:
    logger.info(f'Вызов ноды generate_sql_query')
    client = get_openai_client()
    settings = get_settings()

    # https://developers.openai.com/api/reference/python/resources/responses/methods/create
    response = await client.responses.create(
        model=settings.openai_model,
        instructions=SQL_SYSTEM_PROMPT,
        input=state['prompt'],
    )
    sql_query = clean_sql_query(response.output_text)
    logger.info(f'AI-агент сгенерировал SQL-запрос: {sql_query}')

    return {
        **state,
        'sql_query': sql_query
    }


async def execute_sql(state: AgentState) -> AgentState:
    sql_query = state['sql_query']

    if not sql_query:
        logger.error('SQL-запрос пустой')
        raise ValueError('SQL-запрос пустой')
    
    logger.info(f'Вызов ноды execute_sql запроса: {sql_query}')

    sql_result = await execute_sql_query(sql_query)
    sql_result_json = json.dumps(sql_result, ensure_ascii=False, default=str)
    logger.info(f'Результат выполнения SQL-запроса: {sql_result_json}')

    return {
        **state,
        'sql_result': sql_result_json,
    }

async def answer(state: AgentState) -> AgentState:
    logger.info(f'Вызов ноды answer')

    client = get_openai_client()
    settings = get_settings()

    USER_PROMPT = f"""
        Пользователь задал вопрос: {state['prompt']}
        SQL-запрос, который был выполнен: {state['sql_query']}
        Результат выполнения SQL-запроса (в формате JSON): {state['sql_result']}
    """

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=ANSWER_SYSTEM_PROMPT,
        input=USER_PROMPT,
    )
    logger.info(f'AI-агент вернул ответ: {response.output_text}')

    return {
        **state,
        'answer': response.output_text
    }
