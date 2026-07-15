from pathlib import Path
import json
import re

import asyncpg
from config import get_settings
from logger import init_logger

logger = init_logger()

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

TEST_CSV_TABLES = (
    # Эти таблицы устроены как kolesa, но содержат заранее внесенные тестовые ошибки
    ('kolesa_test_easy_001', 'kolesa_test_easy_001.csv'),
    ('kolesa_test_logic_001', 'kolesa_test_logic_001.csv'),
    ('kolesa_test_hard_001', 'kolesa_test_hard_001.csv'),
    ('kolesa_test_mixed_001', 'kolesa_test_mixed_001.csv'),
)

INTERNAL_TABLES = {
    'test_datasets',
    'expected_anomalies',
    'analysis_profiles',
    'analysis_runs',
    'agent_findings',
    'evaluation_results',
    'anomaly_detection_matches',
    'model_price_catalog',
}

DATA_TABLES = {'kolesa'} | {table_name for table_name, _ in TEST_CSV_TABLES}

forbidden_sql_keywords = ['delete', 'update', 'insert', 'drop', 'alter', 'create', 'truncate', 'replace', 'grant', 'revoke', 'commit', 'rollback', 'savepoint', 'lock', 'unlock', 'analyze', 'vacuum', 'explain', 'set', 'reset', 'show', 'begin', 'declare', 'fetch', 'close']
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
    # Строковые литералы и комментарии не должны влиять на проверку SQL-команд
    sql_query = re.sub(r'/\*.*?\*/', ' ', sql_query, flags=re.DOTALL)
    sql_query = re.sub(r'--[^\n]*', ' ', sql_query)
    sql_query = re.sub(r"'(?:''|[^'])*'", ' ', sql_query)
    # Заменяем возможные разделители на пробелы, и разбиваем на слова в список
    translation = str.maketrans({separator: ' ' for separator in sql_word_separators})
    return sql_query.lower().translate(translation).split()


async def copy_csv_to_table(connection, table_name: str, csv_path: str, columns):
    # Общая загрузка CSV для основной и тестовых таблиц
    return await connection.copy_to_table(
        table_name,
        source=csv_path,
        columns=columns,
        format='csv',
        header=True,
        encoding='utf-8',
    )


async def load_csv_table_if_empty(connection, table_name: str, csv_path: str):
    # CSV загружаем один раз, чтобы повторный старт бота не создавал дубли
    has_rows = await connection.fetchval(f'SELECT EXISTS (SELECT 1 FROM {table_name})')
    if has_rows:
        logger.info(f'Таблица {table_name} уже содержит данные; загрузка из CSV пропущена')
        return

    result = await copy_csv_to_table(connection, table_name, csv_path, KOLESA_COLUMNS)
    logger.info(f'Данные из CSV ({csv_path}) загружены в {table_name}: {result}')


async def load_expected_anomalies_records(connection, anomaly_cases_dir: Path):
    # Ожидаемые аномалии грузятся из сгенерированного SQL рядом с тестовыми CSV
    load_sql_path = anomaly_cases_dir / 'expected_anomalies_load.sql'

    if not load_sql_path.exists():
        logger.info(f'Файл загрузки ожидаемых аномалий не найден: {load_sql_path}')
        return

    with load_sql_path.open(encoding='utf-8') as load_sql:
        await connection.execute(load_sql.read())

    logger.info(f'Ожидаемые аномалии загружены из {load_sql_path}')


async def load_test_csv_tables(connection, kolesa_csv_path: str):
    # Тестовые наборы ищем относительно основного CSV, чтобы не добавлять новые пути в .env
    anomaly_cases_dir = Path(kolesa_csv_path).parent / 'anomaly_cases'

    for table_name, csv_filename in TEST_CSV_TABLES:
        csv_path = anomaly_cases_dir / csv_filename

        if not csv_path.exists():
            logger.info(f'Тестовый CSV не найден, загрузка пропущена: {csv_path}')
            continue

        await load_csv_table_if_empty(connection, table_name, str(csv_path))

    await load_expected_anomalies_records(connection, anomaly_cases_dir)


async def init_db():
    settings = get_settings()

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    with open(settings.db_sql_path, encoding='utf-8') as db_sql:
        await connection.execute(db_sql.read())

    await load_csv_table_if_empty(connection, 'kolesa', settings.kolesa_csv_path)
    await load_test_csv_tables(connection, settings.kolesa_csv_path)

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


def validate_query_tables(sql_query: str, allowed_table_name: str):
    # Custom SQL должен читать только таблицу текущего анализа
    query_words = set(get_sql_words(sql_query))
    allowed_table_name = allowed_table_name.lower()

    if allowed_table_name not in query_words:
        logger.error(f'Запрос должен читать таблицу {allowed_table_name}')
        raise ValueError(f'Запрос должен читать таблицу {allowed_table_name}')

    forbidden_tables = INTERNAL_TABLES | (DATA_TABLES - {allowed_table_name})
    used_forbidden_tables = sorted(forbidden_tables & query_words)

    if used_forbidden_tables:
        logger.error(f'Запрос читает запрещенные таблицы: {used_forbidden_tables}')
        raise ValueError('Запрос должен читать только переданную таблицу')


async def get_test_id_by_table_name(table_name: str):
    # Для обычной таблицы kolesa test_id будет None, для тестовых берем его из справочника
    return await fetch_value(
        'SELECT test_id FROM test_datasets WHERE table_name = $1',
        [table_name],
    )


async def save_analysis_profile(table_name: str, schema: dict, profile: dict):
    settings = get_settings()
    # test_id заполнится только для таблиц из test_datasets
    test_id = await get_test_id_by_table_name(table_name)

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Профиль сохраняем целиком, чтобы к проходу можно было вернуться без пересчета
        profile_id = await connection.fetchval(
            '''
            INSERT INTO analysis_profiles (test_id, table_name, row_count, schema_snapshot, profile)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            RETURNING id
            ''',
            test_id,
            table_name,
            profile['row_count'],
            json.dumps(schema, ensure_ascii=False, default=str),
            json.dumps(profile, ensure_ascii=False, default=str),
        )
        return profile_id
    except Exception as e:
        logger.error(f'Ошибка сохранения профиля анализа: {e}')
        raise
    finally:
        await connection.close()


async def save_analysis_run(run_data: dict):
    settings = get_settings()

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Создаем запись прохода до проверок, чтобы findings ссылались на один run_id
        run_id = await connection.fetchval(
            '''
            INSERT INTO analysis_runs (
                test_id,
                profile_id,
                table_name,
                agent_name,
                model_provider,
                model_name,
                prompt_version,
                code_version,
                status,
                metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            RETURNING id
            ''',
            run_data.get('test_id'),
            run_data.get('profile_id'),
            run_data['table_name'],
            run_data.get('agent_name', 'analyze_graph'),
            run_data.get('model_provider', 'openai'),
            run_data['model_name'],
            run_data.get('prompt_version'),
            run_data.get('code_version'),
            run_data.get('status', 'started'),
            json.dumps(run_data.get('metadata', {}), ensure_ascii=False, default=str),
        )
        return run_id
    except Exception as e:
        logger.error(f'Ошибка сохранения прохода анализа: {e}')
        raise
    finally:
        await connection.close()


async def finish_analysis_run(run_id: int, status: str, metadata=None, error_message=None):
    settings = get_settings()

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Завершаем проход и кладем итоговые счетчики/результаты в metadata
        await connection.execute(
            '''
            UPDATE analysis_runs
            SET
                finished_at = now(),
                status = $2,
                error_message = $3,
                metadata = metadata || $4::jsonb
            WHERE id = $1
            ''',
            run_id,
            status,
            error_message,
            json.dumps(metadata or {}, ensure_ascii=False, default=str),
        )
    except Exception as e:
        logger.error(f'Ошибка обновления прохода анализа: {e}')
        raise
    finally:
        await connection.close()


def build_agent_finding_rows(run_id: int, findings: list[dict]):
    # Findings с примерами строк сохраняем как конкретные найденные аномалии
    rows = []

    for finding in findings:
        # Агрегатный finding без sample_rows сохраняем одной строкой без row_id
        sample_rows = expand_finding_sample_rows(finding)

        for sample_index, sample_row in enumerate(sample_rows):
            row_id = sample_row.get('id') if sample_row else None
            kolesa_id = sample_row.get('kolesa_id') if sample_row else None
            # finding_key помогает быстро отличить записи внутри одного прохода
            finding_key_parts = [finding['type'], finding['table'], finding['column'] or '']

            if row_id is not None:
                finding_key_parts.append(str(row_id))
            else:
                finding_key_parts.append(str(sample_index))

            evidence = {
                'count': finding['count'],
                'reason': finding['reason'],
                'sample_row': sample_row,
                'details': finding['details'],
            }

            rows.append((
                run_id,
                ':'.join(finding_key_parts),
                finding['table'],
                finding['column'],
                finding['type'],
                finding['importance'],
                row_id,
                kolesa_id,
                finding['reason'],
                json.dumps(evidence, ensure_ascii=False, default=str),
                json.dumps(finding, ensure_ascii=False, default=str),
            ))

    return rows


def expand_finding_sample_rows(finding: dict):
    # Разворачиваем sample_ids из агрегатных custom SQL в отдельные строки findings
    sample_rows = finding.get('sample_rows') or [None]
    expanded_rows = []

    for sample_row in sample_rows:
        if not isinstance(sample_row, dict):
            expanded_rows.append(sample_row)
            continue

        if sample_row.get('id') is not None or sample_row.get('kolesa_id') is not None:
            expanded_rows.append(sample_row)
            continue

        sample_ids = sample_row.get('sample_ids')
        if not isinstance(sample_ids, list) or not sample_ids:
            expanded_rows.append(sample_row)
            continue

        sample_kolesa_ids = sample_row.get('sample_kolesa_ids') or []
        sample_urls = sample_row.get('sample_urls') or []

        for sample_index, row_id in enumerate(sample_ids):
            expanded_row = dict(sample_row)
            expanded_row['id'] = row_id

            if sample_index < len(sample_kolesa_ids):
                expanded_row['kolesa_id'] = sample_kolesa_ids[sample_index]

            if sample_index < len(sample_urls):
                expanded_row['kolesa_url'] = sample_urls[sample_index]

            expanded_rows.append(expanded_row)

    return expanded_rows


async def save_agent_findings(run_id: int, findings: list[dict]):
    settings = get_settings()
    rows = build_agent_finding_rows(run_id, findings)

    if not rows:
        return 0

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Перезапись делает повторный вызов для того же run_id предсказуемым
        await connection.execute('DELETE FROM agent_findings WHERE run_id = $1', run_id)
        # Сохраняем найденные аномалии пачкой, по одной строке на пример объявления
        await connection.executemany(
            '''
            INSERT INTO agent_findings (
                run_id,
                finding_key,
                table_name,
                column_name,
                anomaly_type,
                importance,
                row_id,
                kolesa_id,
                description,
                evidence,
                raw_finding
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
            ''',
            rows,
        )
        return len(rows)
    except Exception as e:
        logger.error(f'Ошибка сохранения найденных аномалий: {e}')
        raise
    finally:
        await connection.close()


async def get_evaluation_context(run_id: int):
    # Оценка нужна только для проходов, связанных с тестовым датасетом
    run = await fetch_one(
        '''
        SELECT id, test_id, table_name, metadata->>'final_answer' AS final_answer
        FROM analysis_runs
        WHERE id = $1
        ''',
        [run_id],
    )

    if not run or not run['test_id']:
        return None

    expected_anomalies = await fetch_all(
        '''
        SELECT
            id,
            anomaly_key,
            table_name,
            source_row_number,
            kolesa_id,
            anomaly_type,
            difficulty,
            changed_columns,
            row_selector,
            expected_detection,
            description
        FROM expected_anomalies
        WHERE test_id = $1
        ORDER BY id
        ''',
        [run['test_id']],
    )
    agent_findings = await fetch_all(
        '''
        SELECT
            id,
            finding_key,
            table_name,
            column_name,
            anomaly_type,
            importance,
            row_id,
            kolesa_id,
            description,
            evidence
        FROM agent_findings
        WHERE run_id = $1
        ORDER BY id
        ''',
        [run_id],
    )

    # asyncpg возвращает jsonb строкой, поэтому декодируем поля для matcher
    for anomaly in expected_anomalies:
        anomaly['row_selector'] = json.loads(anomaly['row_selector'])

    for finding in agent_findings:
        finding['evidence'] = json.loads(finding['evidence'])

    return {
        'run': run,
        'expected_anomalies': expected_anomalies,
        'agent_findings': agent_findings,
    }


async def save_evaluation_result(run_id: int, test_id: str, evaluation_data: dict):
    settings = get_settings()
    matches = evaluation_data['matches']
    extra_finding_ids = evaluation_data['extra_finding_ids']

    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        # Один проход может переоцениваться, поэтому старую оценку удаляем каскадом
        await connection.execute('DELETE FROM evaluation_results WHERE run_id = $1', run_id)
        evaluation_result_id = await connection.fetchval(
            '''
            INSERT INTO evaluation_results (
                run_id,
                test_id,
                matcher_name,
                matcher_model_provider,
                matcher_model_name,
                expected_total,
                expected_found,
                expected_recall,
                agent_findings_count,
                matched_findings_count,
                missed_count,
                extra_findings_count,
                report_expected_found,
                report_expected_recall,
                report_missed_count,
                raw_matches
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb)
            RETURNING id
            ''',
            run_id,
            test_id,
            evaluation_data['matcher_name'],
            evaluation_data['matcher_model_provider'],
            evaluation_data['matcher_model_name'],
            evaluation_data['expected_total'],
            evaluation_data['expected_found'],
            evaluation_data['expected_recall'],
            evaluation_data['agent_findings_count'],
            evaluation_data['matched_findings_count'],
            evaluation_data['missed_count'],
            evaluation_data['extra_findings_count'],
            evaluation_data['report_expected_found'],
            evaluation_data['report_expected_recall'],
            evaluation_data['report_missed_count'],
            json.dumps(evaluation_data['raw_matches'], ensure_ascii=False, default=str),
        )

        match_rows = []
        for match in matches:
            match_rows.append((
                evaluation_result_id,
                match['expected_anomaly_id'],
                match.get('agent_finding_id'),
                match['match_status'],
                match.get('confidence'),
                match.get('matcher_reason'),
                match.get('report_match_status'),
                match.get('report_matcher_reason'),
            ))

        for finding_id in extra_finding_ids:
            match_rows.append((
                evaluation_result_id,
                None,
                finding_id,
                'extra',
                None,
                'Finding не сопоставлена с внесенными тестовыми аномалиями',
                None,
                None,
            ))

        if match_rows:
            await connection.executemany(
                '''
                INSERT INTO anomaly_detection_matches (
                    evaluation_result_id,
                    expected_anomaly_id,
                    agent_finding_id,
                    match_status,
                    confidence,
                    matcher_reason,
                    report_match_status,
                    report_matcher_reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ''',
                match_rows,
            )

        return evaluation_result_id
    except Exception as e:
        logger.error(f'Ошибка сохранения оценки анализа: {e}')
        raise
    finally:
        await connection.close()



async def execute_sql_query(sql_query: str, allowed_table_name: str = 'kolesa'):
    settings = get_settings()

    query = await validate_readonly_query(sql_query)

    validate_query_tables(query, allowed_table_name)
    
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
