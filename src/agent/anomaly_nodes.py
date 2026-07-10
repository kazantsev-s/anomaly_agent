from collections import Counter
from html import escape
import json

from openai import AsyncOpenAI

from agent.state import AnomalyAgentState
from agent.tools import get_schema, profile_table, run_standard_checks
from config import get_settings
from logger import init_logger


logger = init_logger()


REPORT_FINDINGS_LIMIT = 12
REPORT_SAMPLE_ROWS_LIMIT = 3

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

    Объясняй только аномалии из входного JSON.
    Не придумывай новые findings, новые колонки, новые строки и новые причины.
    Если findings пустой, напиши, что базовые проверки не нашли аномалий.
    Держи ответ короче 3500 символов.

    Структура ответа:
    1. Краткое резюме.
    2. Основные проблемы.
    3. Детальные находки.
    4. Что проверить вручную.
    5. Итоговая оценка качества данных.
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
