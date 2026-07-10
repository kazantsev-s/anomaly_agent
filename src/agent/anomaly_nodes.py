from collections import Counter
from html import escape

from agent.state import AnomalyAgentState
from agent.tools import get_schema, profile_table, run_standard_checks


IMPORTANCE_ORDER = {
    'high': 0,
    'medium': 1,
    'low': 2,
}

IMPORTANCE_LABELS = {
    'high': 'высокая',
    'medium': 'средняя',
    'low': 'низкая',
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


def get_table_name(state: AnomalyAgentState) -> str:
    return state.get('table_name') or 'kolesa'


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(
        findings,
        key=lambda finding: (
            IMPORTANCE_ORDER.get(finding.get('IMPORTANCE'), 3),
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


def format_detail_values(finding: dict) -> list[str]:
    details = finding.get('details') or {}
    values = details.get('values') or details.get('duplicates') or []

    return [
        f"{format_value(row.get('value'))}: {format_value(row.get('count'))}"
        for row in values[:3]
    ]


def get_finding_label(finding_type: str) -> str:
    return FINDING_TYPE_LABELS.get(finding_type, finding_type)


async def load_schema(state: AnomalyAgentState) -> AnomalyAgentState:
    table_name = get_table_name(state)
    schema = await get_schema(table_name)

    return {
        **state,
        'table_name': table_name,
        'schema': schema,
    }


async def profile_table_node(state: AnomalyAgentState) -> AnomalyAgentState:
    table_name = get_table_name(state)
    profile = await profile_table(table_name)

    return {
        **state,
        'profile': profile,
    }


async def run_standard_checks_node(state: AnomalyAgentState) -> AnomalyAgentState:
    table_name = get_table_name(state)
    standard_findings = await run_standard_checks(table_name)

    return {
        **state,
        'standard_findings': standard_findings,
    }


async def merge_findings(state: AnomalyAgentState) -> AnomalyAgentState:
    return {
        **state,
        'all_findings': list(state.get('standard_findings') or []),
    }


async def final_anomaly_answer(state: AnomalyAgentState) -> AnomalyAgentState:
    profile = state.get('profile') or {}
    findings = sort_findings(state.get('all_findings') or [])
    row_count = profile.get('row_count', 0)

    lines = [
        '<b>Анализ kolesa завершен</b>',
        '',
        f'Проверено строк: <b>{format_value(row_count)}</b>',
        f'Найдено проблем: <b>{format_value(len(findings))}</b>',
    ]

    if not findings:
        lines.append('')
        lines.append('Базовые проверки не нашли аномалий.')
        return {
            **state,
            'answer': '\n'.join(lines),
        }

    type_counts = Counter(get_finding_label(finding['type']) for finding in findings)
    IMPORTANCE_counts = Counter(finding['IMPORTANCE'] for finding in findings)

    lines.extend([
        '',
        '<b>По важности</b>',
    ])

    for IMPORTANCE in ['high', 'medium', 'low']:
        count = IMPORTANCE_counts.get(IMPORTANCE, 0)
        if count:
            lines.append(f'{IMPORTANCE_LABELS[IMPORTANCE]}: {count}')

    lines.extend([
        '',
        '<b>Типы проблем</b>',
    ])

    for label, count in type_counts.most_common(8):
        lines.append(f'{format_value(label)}: {count}')

    lines.extend([
        '',
        '<b>Главные находки</b>',
    ])

    for finding in findings[:6]:
        IMPORTANCE = IMPORTANCE_LABELS.get(finding.get('IMPORTANCE'), finding.get('IMPORTANCE'))
        column = finding.get('column') or 'таблица'
        lines.append(
            f"- <b>{format_value(column)}</b>: {format_value(finding['reason'])} "
            f"({format_value(finding['count'])}, важность: {format_value(IMPORTANCE)})"
        )

    lines.extend([
        '',
        '<b>Примеры</b>',
    ])

    example_lines = []
    seen_examples = set()
    for finding in findings:
        for row in (finding.get('sample_rows') or [])[:2]:
            example_key = row.get('kolesa_url') or row.get('kolesa_id') or row.get('id')
            if example_key in seen_examples:
                continue

            seen_examples.add(example_key)
            example_lines.append(f"- {format_sample_row(row)}")

        if not finding.get('sample_rows'):
            for value in format_detail_values(finding):
                example_key = f"{finding.get('column')}:{value}"
                if example_key in seen_examples:
                    continue

                seen_examples.add(example_key)
                example_lines.append(f"- {format_value(finding.get('column'))}: {value}")

        if len(example_lines) >= 6:
            break

    lines.extend(example_lines[:6] if example_lines else ['Нет отдельных строк-примеров для верхних находок.'])

    return {
        **state,
        'answer': '\n'.join(lines),
    }
