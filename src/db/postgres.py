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


async def execute_sql_query(sql_query: str):
    settings = get_settings()

    query = sql_query.strip().rstrip(';').strip()

    # Проверка запроса

    if ';' in query:
        logger.error('Запрос не должен содержать ;')
        raise ValueError('Запрос не должен содержать ;')
    
    if not query.lower().startswith(('select', 'with')):
        logger.error('Запрос должен быть SELECT-запросом')
        raise ValueError('Запрос должен быть SELECT-запросом')

    if any(keyword in query.lower() for keyword in forbidden_sql_keywords):
        logger.error('Запрос содержит запрещенные ключевые слова')
        raise ValueError('Запрос содержит запрещенные ключевые слова')

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