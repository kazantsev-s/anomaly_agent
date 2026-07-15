from db.postgres import execute_sql_query, fetch_all, fetch_value, get_table_columns, validate_table_name


# Настройки базовых проверок: какие колонки считать важными и как их проверять.
ROW_CONTEXT_COLUMNS = ['id', 'kolesa_id', 'brand', 'model', 'year', 'mileage', 'city', 'price', 'kolesa_url']
IMPORTANT_COLUMNS = ['kolesa_id', 'kolesa_url', 'brand', 'model', 'year', 'city', 'fuel_type', 'mileage', 'price']
NUMERIC_CHECK_COLUMNS = ['price', 'mileage', 'year', 'engine_volume', 'imgs_count']
NON_NEGATIVE_COLUMNS = ['price', 'mileage', 'year', 'engine_volume', 'imgs_count']
RARE_CATEGORY_COLUMNS = ['body_type', 'fuel_type', 'transmission', 'drive_type', 'steering_wheel', 'kz_registration']

ALLOWED_CATEGORY_VALUES = {
    'fuel_type': ['бензин', 'дизель', 'газ-бензин', 'гибрид', 'газ', 'электрический'],
    'transmission': ['Автомат', 'Механика', 'Вариатор', 'Робот'],
    'drive_type': ['Передний привод', 'Полный привод', 'Задний привод'],
    'steering_wheel': ['Слева', 'Справа'],
    'kz_registration': ['Да', 'Нет'],
}


# Вспомогательные функции для типов колонок

def is_numeric_type(data_type: str):
    return True if data_type in ['smallint', 'integer', 'bigint', 'numeric'] else False


def is_text_type(data_type: str):
    return True if data_type in ['text'] else False


def is_datetime_type(data_type: str):
    return True if data_type in ['date', 'timestamp without time zone', 'timestamp with time zone'] else False


def is_boolean_type(data_type: str):
    return True if data_type in ['boolean'] else False


# Вспомогательные функции для колонок и SQL-фрагментов

def get_column_names(columns: list[dict]):
    return [column['column_name'] for column in columns]


def clean_text_sql(column_name: str):
    # E'' в Postgres включает escape-последовательности: \n, \r, \t.
    # btrim чистит эти символы только по краям строки, не внутри текста.
    return f"btrim({column_name}, E' \\n\\r\\t')"


def get_select_columns(schema: dict, extra_columns=None):
    # В примеры строк добавляем базовый контекст объявления и конкретную колонку проверки.
    extra_columns = extra_columns or []
    selected_columns = []

    for column_name in ROW_CONTEXT_COLUMNS + extra_columns:
        if column_name not in selected_columns:
            selected_columns.append(column_name)

    return selected_columns


# Вспомогательная функция для структуры результатов проверок

def make_finding(anomaly_type: str, table_name: str, column_name: str, importance: str, count: int, reason: str, sample_rows=None, details=None):
    # Единый формат результата
    return {
        'type': anomaly_type,
        'table': table_name,
        'column': column_name,
        'importance': importance,
        'count': count,
        'reason': reason,
        'sample_rows': sample_rows or [],
        'details': details or {},
    }


async def get_sample_rows(table_name: str, schema: dict, condition: str, extra_columns=None, limit: int = 10, params=None):
    # Возвращаем несколько строк-примеров, чтобы отчет мог показать конкретные объявления
    selected_columns = get_select_columns(schema, extra_columns)

    select_sql = ', '.join(selected_columns)

    return await fetch_all(f'SELECT {select_sql} FROM {table_name} WHERE {condition} ORDER BY id LIMIT {limit}', params)


# Инструменты для чтения схемы и профиля таблицы

async def get_schema(table_name: str):
    table_name = await validate_table_name(table_name)
    columns = await get_table_columns(table_name)

    # Разделяем колонки по типам, чтобы дальше считать подходящие статистики.
    numeric_columns = []
    text_columns = []
    datetime_columns = []
    boolean_columns = []

    for column in columns:
        column_name = column['column_name']
        data_type = column['data_type']

        if is_numeric_type(data_type):
            numeric_columns.append(column_name)
        elif is_text_type(data_type):
            text_columns.append(column_name)
        elif is_datetime_type(data_type):
            datetime_columns.append(column_name)
        elif is_boolean_type(data_type):
            boolean_columns.append(column_name)

    return {
        'table_name': table_name,
        'columns': columns,
        'column_names': get_column_names(columns),
        'numeric_columns': numeric_columns,
        'text_columns': text_columns,
        'datetime_columns': datetime_columns,
        'boolean_columns': boolean_columns,
    }


async def profile_table(schema: dict):
    # Собираем общий обзор таблицы: размер и статистики по колонкам.
    table_sql = schema['table_name']

    row_count = await fetch_value(f'SELECT COUNT(*) FROM {table_sql}')

    profile = {
        'table_name': schema['table_name'],
        'row_count': row_count,
        'columns': {},
    }

    for column in schema['columns']:
        # Базовые метрики есть у каждой колонки, типовые метрики зависят от типа данных
        column_name = column['column_name']
        data_type = column['data_type']

        column_profile = {
            'data_type': data_type,
            'null_count': await fetch_value(f'SELECT COUNT(*) FROM {table_sql} WHERE {column_name} IS NULL'),
            'unique_count': await fetch_value(f'SELECT COUNT(DISTINCT {column_name}) FROM {table_sql}'),
        }

        if is_numeric_type(data_type):
            stats = await fetch_all(
                f'''
                SELECT
                    MIN({column_name}) AS min_value,
                    MAX({column_name}) AS max_value,
                    AVG({column_name}) AS avg_value,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY {column_name}) AS median_value,
                    STDDEV_SAMP({column_name}) AS stddev_value
                FROM {table_sql} WHERE {column_name} IS NOT NULL
                '''
            )
            column_profile['numeric_stats'] = stats[0] if stats else {}

        elif is_text_type(data_type):
            clean_column_name = clean_text_sql(column_name)
            column_profile['empty_count'] = await fetch_value(
                f"SELECT COUNT(*) FROM {table_sql} WHERE {column_name} IS NOT NULL AND {clean_column_name} = ''"
            )
            column_profile['unknown_count'] = await fetch_value(
                f"SELECT COUNT(*) FROM {table_sql} WHERE lower({clean_column_name}) = 'unknown'"
            )
            column_profile['top_values'] = await fetch_all(
                f'''
                SELECT {clean_column_name}::text AS value, COUNT(*) AS count
                FROM {table_sql} WHERE {column_name} IS NOT NULL
                GROUP BY value ORDER BY count DESC
                LIMIT 5
                '''
            )

        elif is_datetime_type(data_type):
            stats = await fetch_all(
                f'''
                SELECT
                    MIN({column_name}) AS min_value,
                    MAX({column_name}) AS max_value
                FROM {table_sql} WHERE {column_name} IS NOT NULL
                '''
            )
            column_profile['datetime_stats'] = stats[0] if stats else {}

        elif is_boolean_type(data_type):
            column_profile['top_values'] = await fetch_all(
                f'''
                SELECT {column_name}::text AS value, COUNT(*) AS count
                FROM {table_sql} WHERE {column_name} IS NOT NULL
                GROUP BY {column_name} ORDER BY count DESC
                LIMIT 5
                '''
            )

        profile['columns'][column_name] = column_profile

    return profile


# Проверки пропусков в ключевых колонках

async def check_missing_values(schema: dict, profile: dict):
    table_name = schema['table_name']
    findings = []

    # Проверяем только важные колонки, т.к. есть и необязательные поля, по типу generation.
    for column_name in IMPORTANT_COLUMNS:
        column_info = profile['columns'][column_name]
        importance = 'high' if column_name in ['kolesa_id', 'brand', 'model', 'year', 'price'] else 'medium'

        if column_info['null_count'] > 0:
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                f'{column_name} IS NULL',
                [column_name],
            )
            findings.append(make_finding(
                'missing_null',
                table_name,
                column_name,
                importance,
                column_info['null_count'],
                f'В важной колонке {column_name} есть NULL-значения',
                sample_rows,
            ))

        if is_text_type(column_info['data_type']) and column_info['empty_count'] > 0:
            clean_column_sql = clean_text_sql(column_name)
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                f"{column_name} IS NOT NULL AND {clean_column_sql} = ''",
                [column_name],
            )
            findings.append(make_finding(
                'missing_empty_text',
                table_name,
                column_name,
                importance,
                column_info['empty_count'],
                f'В важной текстовой колонке {column_name} есть пустые строки',
                sample_rows,
            ))

        if is_text_type(column_info['data_type']) and column_info['unknown_count'] > 0:
            clean_column_sql = clean_text_sql(column_name)
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                f"lower({clean_column_sql}) = 'unknown'",
                [column_name],
            )
            findings.append(make_finding(
                'missing_unknown_text',
                table_name,
                column_name,
                'low',
                column_info['unknown_count'],
                f'В колонке {column_name} есть значение unknown, которое похоже на пропуск',
                sample_rows,
            ))

    return findings


# Проверки числовых границ и статистических выбросов

async def check_numeric_outliers(schema: dict):
    table_name = schema['table_name']
    findings = []

    for column_name in NUMERIC_CHECK_COLUMNS:
        if column_name in NON_NEGATIVE_COLUMNS:
            count = await fetch_value(f'SELECT COUNT(*) FROM {table_name} WHERE {column_name} < 0')

            if count > 0:
                sample_rows = await get_sample_rows(table_name, schema, f'{column_name} < 0', [column_name])
                findings.append(make_finding(
                    'negative_numeric_value',
                    table_name,
                    column_name,
                    'high',
                    count,
                    f'Колонка {column_name} содержит отрицательные значения',
                    sample_rows,
                ))

        if column_name in ['price', 'engine_volume']:
            count = await fetch_value(f'SELECT COUNT(*) FROM {table_name} WHERE {column_name} <= 0')

            if count > 0:
                sample_rows = await get_sample_rows(table_name, schema, f'{column_name} <= 0', [column_name])
                findings.append(make_finding(
                    'non_positive_numeric_value',
                    table_name,
                    column_name,
                    'high',
                    count,
                    f'Колонка {column_name} содержит нулевые или отрицательные значения',
                    sample_rows,
                ))

        if column_name == 'year':
            condition = 'year < 1950 OR year > EXTRACT(YEAR FROM CURRENT_DATE)::integer + 1'
            count = await fetch_value(f'SELECT COUNT(*) FROM {table_name} WHERE {condition}')

            if count > 0:
                sample_rows = await get_sample_rows(table_name, schema, condition, [column_name])
                findings.append(make_finding(
                    'invalid_car_year',
                    table_name,
                    column_name,
                    'high',
                    count,
                    'Год выпуска выглядит невозможным или слишком далеким от текущей даты',
                    sample_rows,
                ))

        # IQR для получения статистических порогов выбросов
        stats = await fetch_all(
            f'''
            SELECT
                percentile_cont(0.25) WITHIN GROUP (ORDER BY {column_name}) AS q1,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY {column_name}) AS q3
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
            '''
        )
        q1 = stats[0]['q1'] if stats else None
        q3 = stats[0]['q3'] if stats else None

        if q1 is None or q3 is None:
            continue

        iqr = q3 - q1

        if iqr == 0:
            continue

        low_border = q1 - 1.5 * iqr
        high_border = q3 + 1.5 * iqr
        condition = f'{column_name} < {low_border} OR {column_name} > {high_border}'
        count = await fetch_value(f'SELECT COUNT(*) FROM {table_name} WHERE {condition}')

        if count > 0:
            sample_rows = await get_sample_rows(table_name, schema, condition, [column_name])
            findings.append(make_finding(
                'iqr_numeric_outlier',
                table_name,
                column_name,
                'medium',
                count,
                f'В колонке {column_name} есть значения за пределами IQR-диапазона',
                sample_rows,
                {
                    'q1': q1,
                    'q3': q3,
                    'low_border': low_border,
                    'high_border': high_border,
                },
            ))

    return findings


# Проверки категорий, например, на наличие нетиповых, редкий категорий

async def check_categorical_anomalies(schema: dict):
    table_name = schema['table_name']
    findings = []
    row_count = await fetch_value(f'SELECT COUNT(*) FROM {table_name}')
    # Минимальный порог защищает маленькие таблицы от слишком агрессивной редкости.
    rare_threshold = max(2, int(row_count * 0.001))

    for column_name, allowed_values in ALLOWED_CATEGORY_VALUES.items():
        clean_column_sql = clean_text_sql(column_name)
        unexpected_values = await fetch_all(
            f'''
            SELECT {clean_column_sql}::text AS value, COUNT(*) AS count
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
                AND {clean_column_sql} <> ''
                AND {clean_column_sql} <> ALL($1::text[])
            GROUP BY value
            ORDER BY count DESC
            LIMIT 20
            ''',
            [allowed_values],
        )

        if unexpected_values:
            # Для сохранения finding нужны конкретные строки, а не только список значений
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                f'''
                {column_name} IS NOT NULL
                    AND {clean_column_sql} <> ''
                    AND {clean_column_sql} <> ALL($1::text[])
                ''',
                [column_name],
                params=[allowed_values]
            )
            findings.append(make_finding(
                'unexpected_category',
                table_name,
                column_name,
                'high',
                sum(row['count'] for row in unexpected_values),
                f'В колонке {column_name} есть значения вне ожидаемого списка категорий',
                sample_rows,
                {'values': unexpected_values},
            ))

    for column_name in RARE_CATEGORY_COLUMNS:
        clean_column_sql = clean_text_sql(column_name)
        rare_values = await fetch_all(
            f'''
            SELECT {clean_column_sql}::text AS value, COUNT(*) AS count
            FROM {table_name}
            WHERE {column_name} IS NOT NULL AND {clean_column_sql} <> ''
            GROUP BY value
            HAVING COUNT(*) <= $1
            ORDER BY count ASC, value ASC
            LIMIT 20
            ''',
            [rare_threshold],
        )

        if rare_values:
            # Берем примеры строк с редкими значениями, чтобы потом был row_id/kolesa_id
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                f'''
                {column_name} IS NOT NULL
                    AND {clean_column_sql} <> ''
                    AND {clean_column_sql} IN (
                        SELECT {clean_column_sql}::text
                        FROM {table_name}
                        WHERE {column_name} IS NOT NULL AND {clean_column_sql} <> ''
                        GROUP BY {clean_column_sql}
                        HAVING COUNT(*) <= $1
                    )
                ''',
                [column_name],
                params=[rare_threshold],
            )
            findings.append(make_finding(
                'rare_category',
                table_name,
                column_name,
                'low',
                sum(row['count'] for row in rare_values),
                f'В колонке {column_name} есть редкие категории',
                sample_rows,
                {
                    'rare_threshold': rare_threshold,
                    'values': rare_values,
                },
            ))

    return findings


# Проверки уникальности идентификаторов объявлений (id и ссылка)

async def check_duplicates(table_name: str):
    findings = []

    for column_name in ['kolesa_id', 'kolesa_url']:
        duplicates = await fetch_all(
            f'''
            SELECT {column_name}::text AS value, COUNT(*) AS count
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
            GROUP BY {column_name}
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            LIMIT 20
            '''
        )

        if duplicates:
            # Для дублей сохраняем сами объявления, а агрегат остается в details
            sample_rows = await get_sample_rows(
                table_name,
                {'table_name': table_name},
                f'''
                {column_name} IN (
                    SELECT {column_name}
                    FROM {table_name}
                    WHERE {column_name} IS NOT NULL
                    GROUP BY {column_name}
                    HAVING COUNT(*) > 1
                )
                ''',
                [column_name],
            )
            findings.append(make_finding(
                'duplicate_value',
                table_name,
                column_name,
                'high',
                sum(row['count'] for row in duplicates),
                f'В колонке {column_name} есть повторяющиеся значения',
                sample_rows,
                {'duplicates': duplicates},
            ))

    return findings


# Проверки согласованности между связанными колонками

async def check_logic_rules(schema: dict):
    table_name = schema['table_name']
    findings = []

    logic_rules = [
        {
            'name': 'found_img_false_but_url_exists',
            'columns': ['found_img', 'img_url'],
            'column': 'found_img',
            'condition': f"found_img = false AND img_url IS NOT NULL AND {clean_text_sql('img_url')} <> ''",
            'importance': 'medium',
            'reason': 'found_img = false, но ссылка на изображение заполнена',
        },
        {
            'name': 'found_img_true_but_url_missing',
            'columns': ['found_img', 'img_url'],
            'column': 'found_img',
            'condition': f"found_img = true AND (img_url IS NULL OR {clean_text_sql('img_url')} = '')",
            'importance': 'high',
            'reason': 'found_img = true, но ссылка на изображение пустая',
        },
        {
            'name': 'zero_images_but_image_found',
            'columns': ['imgs_count', 'found_img'],
            'column': 'imgs_count',
            'condition': 'imgs_count = 0 AND found_img = true',
            'importance': 'medium',
            'reason': 'Количество фото равно 0, но флаг found_img показывает, что фото найдено',
        },
        {
            'name': 'images_count_positive_but_image_not_found',
            'columns': ['imgs_count', 'found_img'],
            'column': 'imgs_count',
            'condition': 'imgs_count > 0 AND found_img = false',
            'importance': 'medium',
            'reason': 'Количество фото больше 0, но found_img = false',
        },
        {
            'name': 'fresh_car_high_mileage',
            'columns': ['year', 'mileage'],
            'column': 'mileage',
            'condition': 'year >= EXTRACT(YEAR FROM CURRENT_DATE)::integer - 1 AND mileage > 200000',
            'importance': 'medium',
            'reason': 'Очень свежий автомобиль имеет подозрительно большой пробег',
        },
        {
            'name': 'electric_car_positive_engine_volume',
            'columns': ['fuel_type', 'engine_volume'],
            'column': 'engine_volume',
            'condition': f"lower({clean_text_sql('fuel_type')}) = 'электрический' AND engine_volume > 0",
            'importance': 'high',
            'reason': 'У электрического автомобиля указан ненулевой объем двигателя',
        },
        {
            'name': 'non_electric_car_zero_engine_volume',
            'columns': ['fuel_type', 'engine_volume'],
            'column': 'engine_volume',
            'condition': f"lower({clean_text_sql('fuel_type')}) <> 'электрический' AND engine_volume = 0",
            'importance': 'high',
            'reason': 'У неэлектрического автомобиля указан нулевой объем двигателя',
        },
    ]

    for rule in logic_rules:
        count = await fetch_value(f"SELECT COUNT(*) FROM {table_name} WHERE {rule['condition']}")

        if count > 0:
            sample_rows = await get_sample_rows(
                table_name,
                schema,
                rule['condition'],
                rule['columns'],
            )
            findings.append(make_finding(
                rule['name'],
                table_name,
                rule['column'],
                rule['importance'],
                count,
                rule['reason'],
                sample_rows,
            ))

    return findings


async def run_custom_sql(sql_query: str, table_name: str):
    return await execute_sql_query(sql_query, table_name)


async def run_standard_checks(schema: dict, profile: dict):
    table_name = schema['table_name']

    # Запускаем все первичные проверки
    findings = []
    findings.extend(await check_missing_values(schema, profile))
    findings.extend(await check_numeric_outliers(schema))
    findings.extend(await check_categorical_anomalies(schema))
    findings.extend(await check_duplicates(table_name))
    findings.extend(await check_logic_rules(schema))

    return findings
