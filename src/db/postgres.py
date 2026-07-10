import logging
import asyncpg
from config import get_settings

logger = logging.getLogger(__name__)
KOLESA_COLUMNS = (
    'kolesa_id',
    'kolesa_url',
    'parsed_at',
    'brand',
    'model',
    'generation',
    'year',
    'city',
    'body_type',
    'fuel_type',
    'engine_volume',
    'mileage',
    'transmission',
    'drive_type',
    'steering_wheel',
    'color',
    'kz_registration',
    'imgs_count',
    'price',
    'img_filename',
    'img_url',
    'found_img',
)

forbidden_sql_keywords = ['delete', 'update', 'insert', 'drop', 'alter', 'create', 'truncate', 'replace', 'grant', 'revoke', 'commit', 'rollback', 'savepoint', 'lock', 'unlock', 'analyze', 'vacuum', 'explain', 'set', 'reset', 'show', 'begin', 'end', 'declare', 'fetch', 'close']   
sql_word_separators = ' \n\t\r,.;()[]{}+-*/=%<>!?:\'"`'


def normalize_params(params=None):
    # asyncpg принимает параметры как отдельные позиционные аргументы:
    # connection.fetch(query, param1, param2). Эта функция приводит вход
    # к единому формату, чтобы fetch_all/fetch_one/fetch_value были удобнее
    if params is None:
        return []

    if isinstance(params, (list, tuple)):
        return list(params)

    return [params]


def get_sql_words(sql_query: str):
    # Заменяем возможные разделители на пробелы, и разбиваем на слова в список
    translation = str.maketrans({separator: ' ' for separator in sql_word_separators})
    return sql_query.lower().translate(translation).split()


async def init_db():
    settings = get_settings()

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    with open(settings.kolesa_table_sql_path, encoding='utf-8') as table_sql:
        await connection.execute(table_sql.read())

    has_rows = await connection.fetchval('SELECT EXISTS (SELECT 1 FROM kolesa)')
    if has_rows:
        logger.info('Таблица kolesa уже содержит данные; Наполнение из CSV пропускаем')
        await connection.close()
        return

    result = await connection.copy_to_table(
        'kolesa',
        source=settings.kolesa_csv_path,
        columns=KOLESA_COLUMNS,
        format='csv',
        header=True,
        encoding='utf-8',
    )
    logger.info(f'Данные из CSV ({settings.kolesa_csv_path}) загружены: {result}')

    await connection.close()


async def ping_db():
    settings = get_settings()

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )
    await connection.execute('SELECT 1')
    await connection.close()


async def fetch_all(sql_query: str, params=None):
    settings = get_settings()
    query_params = normalize_params(params)

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Возвращаем множество строк как список dict
        rows = await connection.fetch(sql_query, *query_params, timeout=10)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f'Ошибка выполнения SQL-запроса: {e}')
        raise
    finally:
        await connection.close()


async def fetch_one(sql_query: str, params=None):
    settings = get_settings()
    query_params = normalize_params(params)

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Возвращаем одну строку или None, если запрос ничего не нашел
        row = await connection.fetchrow(sql_query, *query_params, timeout=10)
        return dict(row) if row else None
    except Exception as e:
        logger.error(f'Ошибка выполнения SQL-запроса: {e}')
        raise
    finally:
        await connection.close()


async def fetch_value(sql_query: str, params=None):
    settings = get_settings()
    query_params = normalize_params(params)

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Для запросов с одним значением, например COUNT(*), EXISTS(...) и т.д.
        return await connection.fetchval(sql_query, *query_params, timeout=10)
    except Exception as e:
        logger.error(f'Ошибка выполнения SQL-запроса: {e}')
        raise
    finally:
        await connection.close()


async def get_table_columns(table_name: str):
    table_name = await validate_table_name(table_name)

    # Получаем список колонок, их типы и порядок
    return await fetch_all(
        '''
        SELECT column_name, data_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        ''',
        [table_name]
    )


async def validate_table_name(table_name: str):
    table_name = table_name.strip() if table_name else ''

    if not table_name:
        logger.error('Название таблицы не задано')
        raise ValueError('Название таблицы не задано')

    try:
        table_exists = await fetch_value(
            '''
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
            )
            ''',
            [table_name],
        )
    except Exception as e:
        logger.error(f'Не удалось проверить таблицу: {e}')
        raise ValueError('Не удалось проверить таблицу')

    if not table_exists:
        logger.error(f'Таблица не найдена: {table_name}')
        raise ValueError(f'Таблица не найдена: {table_name}')

    return table_name


async def validate_readonly_query(sql_query: str):
    query = sql_query.strip().rstrip(';').strip()
    query_lower = query.lower()

    if not query:
        logger.error('SQL-запрос пустой')
        raise ValueError('SQL-запрос пустой')

    if ';' in query:
        logger.error('Запрос не должен содержать ;')
        raise ValueError('Запрос не должен содержать ;')
    
    if not query_lower.startswith(('select', 'with')):
        logger.error('Запрос должен быть SELECT-запросом')
        raise ValueError('Запрос должен быть SELECT-запросом')

    # Проверяем запрещенные команды по целым словам
    query_words = set(get_sql_words(query))
    if any(keyword in query_words for keyword in forbidden_sql_keywords):
        logger.error('Запрос содержит запрещенные ключевые слова')
        raise ValueError('Запрос содержит запрещенные ключевые слова')

    return query


async def save_analysis_run(run_data: dict):
    # TODO
    pass


async def save_agent_findings(run_id: int, findings: list[dict]):
    # TODO
    pass


async def execute_sql_query(sql_query: str):
    settings = get_settings()

    query = await validate_readonly_query(sql_query)

    if 'kolesa' not in query.lower():
        logger.error('Запрос должен содержать таблицу kolesa')
        raise ValueError('Запрос должен содержать таблицу kolesa')
    
    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        rows = await connection.fetch(f'SELECT * FROM ({query}) as agent_query LIMIT 25', timeout=10)
        result = [dict(row) for row in rows]
    except Exception as e:
        logger.error(f'Ошибка выполнения SQL-запроса: {e}')
        raise
    finally: 
        await connection.close()

    return result
