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
