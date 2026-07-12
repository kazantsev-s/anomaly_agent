from collections import Counter
from html import escape
import json

from openai import AsyncOpenAI

from agent.state import AnomalyAgentState
from agent.tools import get_schema, profile_table, run_custom_sql, run_standard_checks
from config import get_settings
from logger import init_logger


logger = init_logger()


REPORT_FINDINGS_LIMIT = 12
REPORT_SAMPLE_ROWS_LIMIT = 3
CUSTOM_SQL_PER_ITERATION_LIMIT = 5
CUSTOM_SQL_TOTAL_LIMIT = 10

IMPORTANCE_ORDER = {
    'high': 0,
    'medium': 1,
    'low': 2,
}

FINDING_TYPE_LABELS = {
    'missing_null': 'NULL в важных колонках',
    'missing_empty_text': 'пустые строки',
    'missing_unknown_text': 'unknown как пропуск',
    'negative_numeric_value': 'отрицательные числа',
    'non_positive_numeric_value': 'нулевые или отрицательные значения',
    'invalid_car_year': 'невозможный год выпуска',
    'iqr_numeric_outlier': 'IQR-выбросы',
    'unexpected_category': 'неожиданные категории',
    'rare_category': 'редкие категории',
    'duplicate_value': 'дубликаты',
    'found_img_false_but_url_exists': 'несогласованность found_img/img_url',
    'found_img_true_but_url_missing': 'несогласованность found_img/img_url',
    'zero_images_but_image_found': 'несогласованность imgs_count/found_img',
    'images_count_positive_but_image_not_found': 'несогласованность imgs_count/found_img',
    'fresh_car_high_mileage': 'свежий автомобиль с большим пробегом',
}

ANOMALY_REPORT_SYSTEM_PROMPT = '''
    Ты аналитик качества данных таблицы kolesa с объявлениями Kolesa.kz.
    Отвечай на русском языке.

    Сформируй короткий отчет для Telegram в HTML.
    Используй только простые теги:
    <b>жирный текст</b>
    <i>курсив</i>
    <code>код или значение</code>
    <a href="https://example.com">ссылка</a>

    Не используй Markdown:
    - не пиши **жирный текст**
    - не пиши ### заголовки
    - не используй markdown-таблицы через |

    Объясняй только аномалии и результаты custom SQL из входного JSON.
    Не придумывай новые findings, новые колонки, новые строки и новые причины.
    Если findings пустой, напиши, что базовые проверки не нашли аномалий.
    Не выводи числа без пояснения, что они означают: строк, объявлений, цена, год, пробег, количество фото и т.д.
    Если custom SQL группирует по parsed_at::date, называй это датой парсинга, а не просто датой.
    Разделяй реальные ошибки данных и вероятно валидные редкие случаи.
    Если редкая категория логично соответствует найденным моделям, напиши, что это похоже на валидную редкую категорию, а не на ошибку.
    Если engine_volume = 0 относится к электромобилям, напиши, что это может быть нормальным правилом кодирования для электромобилей.
    Для проблемных объявлений показывай конкретные примеры: id, kolesa_id и ссылку kolesa_url, если они есть во входных данных.
    Если строк много, покажи 2-3 характерных примера или sample_ids/sample_urls из custom SQL, а не весь список.
    Если во входных данных есть только агрегаты без ссылок, честно напиши, что конкретных ссылок в этом блоке нет.
    Держи ответ короче 3500 символов.

    Структура ответа:
    1. Краткое резюме.
    2. Основные проблемы.
    3. Детальные находки.
    4. Что проверить вручную.
    5. Итоговая оценка качества данных.
'''

CUSTOM_CHECK_PLANNER_SYSTEM_PROMPT = '''
    Ты SQL-планировщик дополнительных проверок качества данных таблицы kolesa.
    Тебе передают схему, краткий профиль, базовые findings и уже выполненные custom SQL-запросы.

    Нужно решить, нужны ли дополнительные SELECT-запросы, чтобы точнее объяснить уже найденные аномалии.
    Не ищи абстрактные проблемы вне переданных findings.

    Правила:
    - Верни только JSON без markdown и без объяснений вне JSON.
    - Используй только SELECT или WITH.
    - Запросы должны читать только таблицу kolesa.
    - Не используй INSERT, UPDATE, DELETE, DROP, ALTER, CREATE и другие изменяющие операции.
    - Не ставь точку с запятой в конце.
    - Не делай SELECT *.
    - За одну итерацию предложи не больше 5 SQL-запросов.
    - Если полезных уточнений больше нет, верни need_more_checks: false.
    - Для запросов, которые возвращают конкретные объявления, выбирай id, kolesa_id, brand, model, year, mileage, city, price, kolesa_url.
    - Для агрегатных запросов по группам добавляй примеры объявлений, если это уместно. Например:
      (array_agg(id ORDER BY price DESC))[1:3] AS sample_ids,
      (array_agg(kolesa_url ORDER BY price DESC))[1:3] AS sample_urls.
      Для проверок не по цене выбирай более подходящее поле сортировки.

    Формат ответа:
    {
      "need_more_checks": true,
      "reason": "зачем нужны дополнительные проверки",
      "sql_queries": [
        {
          "name": "short_name",
          "purpose": "что уточняет запрос",
          "query": "SELECT ..."
        }
      ]
    }
'''


def get_table_name(state: AnomalyAgentState) -> str:
    return state.get('table_name') or 'kolesa'


def get_anomaly_openai_client() -> AsyncOpenAI:
    logger.info(f'Вызов ноды get_anomaly_openai_client')
    settings = get_settings()

    if not settings.openai_api_key:
        logger.error('API ключ к OpenAI не задан в переменных окружения')
        raise ValueError('API ключ к OpenAI не задан в переменных окружения')

    return AsyncOpenAI(api_key=settings.openai_api_key)


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(
        findings,
        key=lambda finding: (
            IMPORTANCE_ORDER.get(finding.get('importance'), 3),
            -finding.get('count', 0),
            finding.get('type', ''),
        ),
    )


def format_value(value) -> str:
    if value is None:
        return 'NULL'

    return escape(str(value))


def format_sample_row(row: dict) -> str:
    parts = []

    title = ' '.join(
        part for part in [
            str(row.get('brand') or ''),
            str(row.get('model') or ''),
            str(row.get('year') or ''),
        ]
        if part
    )
    if title:
        parts.append(format_value(title))

    if row.get('price') is not None:
        parts.append(f"цена {format_value(row['price'])}")

    if row.get('mileage') is not None:
        parts.append(f"пробег {format_value(row['mileage'])}")

    if row.get('city'):
        parts.append(format_value(row['city']))

    if row.get('kolesa_url'):
        url = escape(str(row['kolesa_url']), quote=True)
        parts.append(f'<a href="{url}">объявление</a>')
    elif row.get('kolesa_id') is not None:
        parts.append(f"id {format_value(row['kolesa_id'])}")

    return ', '.join(parts) if parts else format_value(row)


def get_finding_label(finding_type: str) -> str:
    return FINDING_TYPE_LABELS.get(finding_type, finding_type)


def build_profile_summary(profile: dict) -> dict:
    logger.info(f'Подготовка краткого профиля таблицы для LLM-отчета')
    columns_summary = {}

    for column_name, column_profile in (profile.get('columns') or {}).items():
        summary = {
            'data_type': column_profile.get('data_type'),
            'null_count': column_profile.get('null_count', 0),
            'unique_count': column_profile.get('unique_count', 0),
        }

        if column_profile.get('numeric_stats'):
            summary['numeric_stats'] = column_profile['numeric_stats']

        if column_profile.get('datetime_stats'):
            summary['datetime_stats'] = column_profile['datetime_stats']

        if 'empty_count' in column_profile:
            summary['empty_count'] = column_profile.get('empty_count', 0)

        if 'unknown_count' in column_profile:
            summary['unknown_count'] = column_profile.get('unknown_count', 0)

        if column_profile.get('top_values'):
            summary['top_values'] = column_profile['top_values'][:5]

        columns_summary[column_name] = summary

    return {
        'row_count': profile.get('row_count', 0),
        'columns': columns_summary,
    }


def compact_sample_rows(sample_rows: list[dict]) -> list[dict]:
    logger.info(f'Подготовка примеров строк для LLM-отчета')
    rows = []

    for row in sample_rows[:REPORT_SAMPLE_ROWS_LIMIT]:
        rows.append({
            'id': row.get('id'),
            'kolesa_id': row.get('kolesa_id'),
            'brand': row.get('brand'),
            'model': row.get('model'),
            'year': row.get('year'),
            'mileage': row.get('mileage'),
            'city': row.get('city'),
            'price': row.get('price'),
            'kolesa_url': row.get('kolesa_url'),
            'text': format_sample_row(row),
        })

    return rows


def build_report_input(state: AnomalyAgentState, findings: list[dict]) -> str:
    logger.info(f'Подготовка входных данных для LLM-отчета')
    schema = state.get('schema') or {}
    profile = state.get('profile') or {}
    top_findings = findings[:REPORT_FINDINGS_LIMIT]

    report_data = {
        'table_name': state.get('table_name') or 'kolesa',
        'schema': {
            'column_names': schema.get('column_names', []),
            'numeric_columns': schema.get('numeric_columns', []),
            'text_columns': schema.get('text_columns', []),
            'datetime_columns': schema.get('datetime_columns', []),
            'boolean_columns': schema.get('boolean_columns', []),
        },
        'profile_summary': build_profile_summary(profile),
        'findings_total': len(findings),
        'findings_in_report': len(top_findings),
        'importance_counts': Counter(finding['importance'] for finding in findings),
        'type_counts': Counter(get_finding_label(finding['type']) for finding in findings),
        'custom_sql_results': state.get('custom_sql_results') or [],
        'findings': [
            {
                'type': finding.get('type'),
                'label': get_finding_label(finding.get('type')),
                'column': finding.get('column'),
                'importance': finding.get('importance'),
                'count': finding.get('count'),
                'reason': finding.get('reason'),
                'sample_rows': compact_sample_rows(finding.get('sample_rows') or []),
                'details': finding.get('details') or {},
            }
            for finding in top_findings
        ],
    }

    report_json = json.dumps(report_data, ensure_ascii=False, default=str)
    logger.info(f'В LLM-отчет передано findings: {len(top_findings)} из {len(findings)}')
    return report_json


def build_custom_check_input(state: AnomalyAgentState) -> str:
    logger.info(f'Подготовка входных данных для планировщика custom SQL')
    findings = sort_findings(state.get('standard_findings') or [])
    custom_sql_results = state.get('custom_sql_results') or []
    custom_sql_count = state.get('custom_sql_count') or 0
    remaining_budget = max(CUSTOM_SQL_TOTAL_LIMIT - custom_sql_count, 0)

    planner_data = {
        'table_name': state.get('table_name') or 'kolesa',
        'remaining_sql_budget': remaining_budget,
        'max_sql_queries_this_iteration': min(CUSTOM_SQL_PER_ITERATION_LIMIT, remaining_budget),
        'schema': {
            'column_names': (state.get('schema') or {}).get('column_names', []),
            'numeric_columns': (state.get('schema') or {}).get('numeric_columns', []),
            'text_columns': (state.get('schema') or {}).get('text_columns', []),
            'datetime_columns': (state.get('schema') or {}).get('datetime_columns', []),
            'boolean_columns': (state.get('schema') or {}).get('boolean_columns', []),
        },
        'profile_summary': build_profile_summary(state.get('profile') or {}),
        'standard_findings': [
            {
                'type': finding.get('type'),
                'label': get_finding_label(finding.get('type')),
                'column': finding.get('column'),
                'importance': finding.get('importance'),
                'count': finding.get('count'),
                'reason': finding.get('reason'),
                'sample_rows': compact_sample_rows(finding.get('sample_rows') or []),
                'details': finding.get('details') or {},
            }
            for finding in findings[:REPORT_FINDINGS_LIMIT]
        ],
        'custom_sql_results': custom_sql_results,
    }

    return json.dumps(planner_data, ensure_ascii=False, default=str)


def parse_llm_json(raw_text: str) -> dict:
    text = raw_text.strip()

    if text.startswith('```json'):
        text = text.removeprefix('```json').strip()

    if text.startswith('```'):
        text = text.removeprefix('```').strip()

    if text.endswith('```'):
        text = text.removesuffix('```').strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.error(f'LLM вернула невалидный JSON для custom SQL-плана: {raw_text}')
        return {
            'need_more_checks': False,
            'reason': 'LLM вернула невалидный JSON',
            'sql_queries': [],
        }

    if not isinstance(result, dict):
        return {
            'need_more_checks': False,
            'reason': 'LLM вернула JSON не в формате объекта',
            'sql_queries': [],
        }

    return result


async def load_schema(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды load_schema')
    table_name = get_table_name(state)
    schema = await get_schema(table_name)
    logger.info(f'Схема таблицы {table_name} загружена')

    return {
        **state,
        'table_name': table_name,
        'schema': schema,
    }


async def profile_table_node(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды profile_table_node')
    table_name = get_table_name(state)
    profile = await profile_table(table_name)
    logger.info(f'Профиль таблицы {table_name} собран')

    return {
        **state,
        'profile': profile,
    }


async def run_standard_checks_node(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды run_standard_checks_node')
    table_name = get_table_name(state)
    standard_findings = await run_standard_checks(table_name)
    logger.info(f'Базовые проверки нашли findings: {len(standard_findings)}')

    return {
        **state,
        'standard_findings': standard_findings,
    }


async def merge_findings(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды merge_findings')
    all_findings = list(state.get('standard_findings') or [])
    logger.info(f'Итоговый список findings содержит записей: {len(all_findings)}')

    return {
        **state,
        'all_findings': all_findings,
    }


async def plan_custom_checks(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды plan_custom_checks')
    custom_sql_count = state.get('custom_sql_count') or 0

    if custom_sql_count >= CUSTOM_SQL_TOTAL_LIMIT:
        logger.info(f'Лимит custom SQL исчерпан: {custom_sql_count}')
        return {
            **state,
            'custom_check_plan': {
                'need_more_checks': False,
                'reason': 'Лимит custom SQL исчерпан',
                'sql_queries': [],
            },
        }

    client = get_anomaly_openai_client()
    settings = get_settings()
    planner_input = build_custom_check_input(state)

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=CUSTOM_CHECK_PLANNER_SYSTEM_PROMPT,
        input=planner_input,
    )
    custom_check_plan = parse_llm_json(response.output_text)
    sql_queries = custom_check_plan.get('sql_queries') or []

    if not isinstance(sql_queries, list):
        sql_queries = []
        custom_check_plan['sql_queries'] = []
        custom_check_plan['need_more_checks'] = False

    logger.info(f'LLM предложила custom SQL-запросов: {len(sql_queries)}')

    return {
        **state,
        'custom_check_plan': custom_check_plan,
    }


async def run_custom_checks(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды run_custom_checks')
    plan = state.get('custom_check_plan') or {}
    custom_sql_results = list(state.get('custom_sql_results') or [])
    custom_sql_count = state.get('custom_sql_count') or 0
    iteration = (state.get('custom_check_iteration') or 0) + 1
    remaining_budget = max(CUSTOM_SQL_TOTAL_LIMIT - custom_sql_count, 0)
    max_queries = min(CUSTOM_SQL_PER_ITERATION_LIMIT, remaining_budget)
    sql_queries = (plan.get('sql_queries') or [])[:max_queries]
    executed_queries = {
        result.get('query')
        for result in custom_sql_results
        if result.get('query')
    }

    for sql_query in sql_queries:
        if not isinstance(sql_query, dict):
            continue

        name = sql_query.get('name') or 'custom_sql'
        query = sql_query.get('query') or ''
        logger.info(f'Выполнение custom SQL-запроса {name}: {query}')

        if query in executed_queries:
            logger.info(f'Custom SQL-запрос {name} уже выполнялся')
            custom_sql_results.append({
                'name': name,
                'purpose': sql_query.get('purpose') or '',
                'query': query,
                'rows': [],
                'error': 'Запрос уже выполнялся',
            })
            custom_sql_count += 1
            continue

        try:
            rows = await run_custom_sql(query)
            executed_queries.add(query)
            custom_sql_results.append({
                'name': name,
                'purpose': sql_query.get('purpose') or '',
                'query': query,
                'rows': rows,
                'error': None,
            })
            logger.info(f'Custom SQL-запрос {name} вернул строк: {len(rows)}')
        except Exception as e:
            logger.error(f'Ошибка выполнения custom SQL-запроса {name}: {e}')
            custom_sql_results.append({
                'name': name,
                'purpose': sql_query.get('purpose') or '',
                'query': query,
                'rows': [],
                'error': str(e),
            })

        custom_sql_count += 1

    return {
        **state,
        'custom_sql_results': custom_sql_results,
        'custom_check_iteration': iteration,
        'custom_sql_count': custom_sql_count,
    }


def route_after_plan(state: AnomalyAgentState) -> str:
    plan = state.get('custom_check_plan') or {}
    sql_queries = plan.get('sql_queries') or []
    custom_sql_count = state.get('custom_sql_count') or 0

    if not plan.get('need_more_checks'):
        return 'final_anomaly_answer'

    if custom_sql_count >= CUSTOM_SQL_TOTAL_LIMIT:
        return 'final_anomaly_answer'

    if not sql_queries:
        return 'final_anomaly_answer'

    return 'run_custom_checks'


async def final_anomaly_answer(state: AnomalyAgentState) -> AnomalyAgentState:
    logger.info(f'Вызов ноды final_anomaly_answer')
    findings = sort_findings(state.get('all_findings') or [])
    client = get_anomaly_openai_client()
    settings = get_settings()
    report_input = build_report_input(state, findings)

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=ANOMALY_REPORT_SYSTEM_PROMPT,
        input=report_input,
    )
    logger.info(f'LLM-отчет по аномалиям сформирован')

    return {
        **state,
        'answer': response.output_text,
    }
