from collections import Counter
import json

from agent.helpers import get_openai_client, load_prompt, render_prompt, strip_markdown_code_block 
from agent.state import AnalyzeAgentState
from agent.tools import get_schema, profile_table, run_custom_sql, run_standard_checks
from config import get_settings
from logger import init_logger


logger = init_logger()


# Порядок важности нужен для единой сортировки findings перед отчетом агента
IMPORTANCE_ORDER = {
    'high': 0,
    'medium': 1,
    'low': 2,
}

# Человекочитаемые названия типов аномалий для входа агента
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


ANOMALY_REPORT_SYSTEM_PROMPT = load_prompt('analyze/anomaly_report_system.prompt.md')
CUSTOM_CHECK_PLANNER_SYSTEM_PROMPT_TEMPLATE = load_prompt('analyze/custom_check_planner_system.prompt.md')


# Вспомогательные функции

# Сортирует findings: сначала важность, затем количество строк
def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda finding: (IMPORTANCE_ORDER[finding['importance']], -finding['count']))


# Оставляет в примерах только поля, полезные для чтения отчета
# Используется при сборке промпта для plan_custom_checks и final_anomaly_answer
def compact_sample_rows(sample_rows: list[dict]) -> list[dict]:
    logger.info(f'Подготовка примеров строк для отчета агента')
    
    settings = get_settings()
    rows = []

    for row in sample_rows[:settings.analyze_report_sample_rows_limit]:
        rows.append({
            'id': row['id'],
            'kolesa_id': row['kolesa_id'],
            'brand': row['brand'],
            'model': row['model'],
            'year': row['year'],
            'mileage': row['mileage'],
            'city': row['city'],
            'price': row['price'],
            'kolesa_url': row['kolesa_url']
        })

    return rows


# Готовит контекст для агента, что планирует дополнительные проверки через запросы
# Используется в ноде plan_custom_checks
def build_custom_check_input(state: AnalyzeAgentState) -> str:
    logger.info(f'Подготовка входных данных для планировщика custom SQL')

    settings = get_settings()
    schema = state['schema']
    findings = sort_findings(state['standard_findings'])
    custom_sql_results = state['custom_sql_results']
    custom_sql_count = state['custom_sql_count']
    remaining_limit = max(settings.analyze_custom_sql_total_limit - custom_sql_count, 0)

    planner_data = {
        'table_name': state['table_name'],
        'remaining_sql_limit': remaining_limit,
        'max_sql_queries_this_iteration': min(settings.analyze_custom_sql_per_iteration_limit, remaining_limit),
        'schema': {
            'column_names': schema['column_names'],
            'numeric_columns': schema['numeric_columns'],
            'text_columns': schema['text_columns'],
            'datetime_columns': schema['datetime_columns'],
            'boolean_columns': schema['boolean_columns']
        },
        'profile': state['profile'],
        'standard_findings': [
            {
                'type': finding['type'],
                'label': FINDING_TYPE_LABELS.get(finding['type'], finding['type']),
                'column': finding['column'],
                'importance': finding['importance'],
                'count': finding['count'],
                'reason': finding['reason'],
                'sample_rows': compact_sample_rows(finding['sample_rows']),
                'details': finding['details']
            }
            for finding in findings[:settings.analyze_report_findings_limit]
        ],
        'custom_sql_results': custom_sql_results
    }

    return json.dumps(planner_data, ensure_ascii=False, default=str)


# Парсит JSON от агента, что формирует план дополнительных SQL-проверок. При невалидном JSON пропускается планирование
# Используется в ноде plan_custom_checks
def parse_llm_json(raw_text: str) -> dict:
    text = strip_markdown_code_block(raw_text, 'json')

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.error(f'Агент вернул невалидный JSON для custom SQL-плана: {raw_text}')
        return {
            'need_more_checks': False,
            'reason': 'Агент вернул невалидный JSON',
            'sql_queries': []
        }

    if not isinstance(result, dict):
        return {
            'need_more_checks': False,
            'reason': 'Агент вернул JSON не в формате объекта',
            'sql_queries': []
        }

    return result


# Роутинг analyze_graph после plan_custom_checks
# Используется как conditional edge в графе
def route_after_plan(state: AnalyzeAgentState) -> str:
    settings = get_settings()
    plan = state['custom_check_plan']
    sql_queries = plan['sql_queries']
    custom_sql_count = state['custom_sql_count']
    custom_check_iterations = state['custom_check_iterations']

    if not plan['need_more_checks']:
        return 'final_anomaly_answer'

    if custom_sql_count >= settings.analyze_custom_sql_total_limit:
        return 'final_anomaly_answer'

    if custom_check_iterations >= settings.analyze_max_custom_check_iterations:
        return 'final_anomaly_answer'

    if not sql_queries:
        return 'final_anomaly_answer'

    return 'run_custom_checks'


# Собирает JSON, из которого агент пишет финальный отчет
# Используется в ноде final_anomaly_answer
def build_report_input(state: AnalyzeAgentState, findings: list[dict]) -> str:
    logger.info(f'Подготовка входных данных для отчета агента')

    settings = get_settings()
    schema = state['schema']
    profile = state['profile']
    top_findings = findings[:settings.analyze_report_findings_limit]

    report_data = {
        'table_name': state['table_name'],
        'schema': {
            'column_names': schema['column_names'],
            'numeric_columns': schema['numeric_columns'],
            'text_columns': schema['text_columns'],
            'datetime_columns': schema['datetime_columns'],
            'boolean_columns': schema['boolean_columns']
        },
        'profile': profile,
        'findings_total': len(findings),
        'findings_in_report': len(top_findings),
        'importance_counts': Counter(finding['importance'] for finding in findings),
        'type_counts': Counter(FINDING_TYPE_LABELS.get(finding['type'], finding['type']) for finding in findings),
        'custom_sql_results': state['custom_sql_results'],
        'findings': [
            {
                'type': finding['type'],
                'label': FINDING_TYPE_LABELS.get(finding['type'], finding['type']),
                'column': finding['column'],
                'importance': finding['importance'],
                'count': finding['count'],
                'reason': finding['reason'],
                'sample_rows': compact_sample_rows(finding['sample_rows']),
                'details': finding['details']
            }
            for finding in top_findings
        ]
    }

    report_json = json.dumps(report_data, ensure_ascii=False, default=str)
    logger.info(f'В отчет агента передано findings: {len(top_findings)} из {len(findings)}')
    return report_json


# Ноды analyze_graph

# Загружает фиксированную схему таблицы kolesa и кладет ее в state
async def load_schema(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды load_schema')

    table_name = state['table_name']
    schema = await get_schema(table_name)
    logger.info(f'Схема таблицы {table_name} загружена')

    return {
        **state,
        'schema': schema
    }


# Собирает профиль таблицы: статистики, категории и примеры значений
async def profile_table_node(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды profile_table_node')

    table_name = state['table_name']
    profile = await profile_table(state['schema'])
    logger.info(f'Профиль таблицы {table_name} собран')

    return {
        **state,
        'profile': profile
    }


# Запускает базовые SQL-проверки и инициализирует счетчики кастомных проверок
async def run_standard_checks_node(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды run_standard_checks_node')

    standard_findings = await run_standard_checks(state['schema'], state['profile'])

    logger.info(f'Базовые проверки нашли findings: {len(standard_findings)}')

    return {
        **state,
        'standard_findings': standard_findings,
        'custom_sql_results': [],
        'custom_sql_count': 0,
        'custom_check_iterations': 0
    }


# Просит агента решить, нужны ли дополнительные SQL-проверки
async def plan_custom_checks(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды plan_custom_checks')

    settings = get_settings()
    custom_sql_count = state['custom_sql_count']
    custom_check_iterations = state['custom_check_iterations']

    # Остановка цикла по планированию запросов, при достижении лимит итераций или запросов

    if custom_check_iterations >= settings.analyze_max_custom_check_iterations:
        logger.info(f'Лимит итераций custom SQL исчерпан: {custom_check_iterations}')

        return {
            **state,
            'custom_check_plan': {
                'need_more_checks': False,
                'reason': 'Лимит итераций custom SQL исчерпан',
                'sql_queries': []
            }
        }

    if custom_sql_count >= settings.analyze_custom_sql_total_limit:
        logger.info(f'Лимит custom SQL исчерпан: {custom_sql_count}')

        return {
            **state,
            'custom_check_plan': {
                'need_more_checks': False,
                'reason': 'Лимит custom SQL исчерпан',
                'sql_queries': []
            }
        }

    # Планировщик получает только сжатый контекст, чтобы не раздувать промпт
    client = get_openai_client()
    planner_input = build_custom_check_input(state)
    planner_system_prompt = render_prompt(
        CUSTOM_CHECK_PLANNER_SYSTEM_PROMPT_TEMPLATE,
        max_sql_queries=settings.analyze_custom_sql_per_iteration_limit
    )

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=planner_system_prompt,
        input=planner_input
    )

    # Агент обязан вернуть объект с флагом need_more_checks и списком SQL
    custom_check_plan = parse_llm_json(response.output_text)
    custom_check_plan.setdefault('need_more_checks', False)
    custom_check_plan.setdefault('reason', '')
    custom_check_plan.setdefault('sql_queries', [])
    sql_queries = custom_check_plan['sql_queries']

    if not isinstance(sql_queries, list):
        sql_queries = []
        custom_check_plan['sql_queries'] = []
        custom_check_plan['need_more_checks'] = False
    else:
        for sql_query in sql_queries:
            if not isinstance(sql_query, dict):
                raise ValueError('Агент вернул custom SQL не в формате объекта')

            for field_name in ['name', 'purpose', 'query']:
                if field_name not in sql_query:
                    raise ValueError(f'В custom SQL-плане нет поля {field_name}')

    logger.info(f'Агент предложил custom SQL-запросов: {len(sql_queries)}')

    return {
        **state,
        'custom_check_plan': custom_check_plan
    }


# Выполняет SQL-запросы, которые предложил агент, и сохраняет результаты
async def run_custom_checks(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды run_custom_checks')

    settings = get_settings()
    plan = state['custom_check_plan']
    custom_sql_results = list(state['custom_sql_results'])
    custom_sql_count = state['custom_sql_count']
    custom_check_iterations = state['custom_check_iterations']
    remaining_limit = max(settings.analyze_custom_sql_total_limit - custom_sql_count, 0)
    max_queries = min(settings.analyze_custom_sql_per_iteration_limit, remaining_limit)
    sql_queries = plan['sql_queries'][:max_queries]

    # Повторные запросы считаем использованной попыткой, но не гоняем в БД снова
    executed_queries = {
        result['query']
        for result in custom_sql_results
    }

    for sql_query in sql_queries:
        name = sql_query['name']
        query = sql_query['query']

        logger.info(f'Выполнение custom SQL-запроса {name}: {query}')

        if query in executed_queries:
            logger.info(f'Custom SQL-запрос {name} уже выполнялся')

            custom_sql_results.append({
                'name': name,
                'purpose': sql_query['purpose'],
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
                'purpose': sql_query['purpose'],
                'query': query,
                'rows': rows,
                'error': None,
            })

            logger.info(f'Custom SQL-запрос {name} вернул строк: {len(rows)}')
        except Exception as e:
            logger.error(f'Ошибка выполнения custom SQL-запроса {name}: {e}')

            custom_sql_results.append({
                'name': name,
                'purpose': sql_query['purpose'],
                'query': query,
                'rows': [],
                'error': str(e),
            })

        custom_sql_count += 1

    return {
        **state,
        'custom_sql_results': custom_sql_results,
        'custom_sql_count': custom_sql_count,
        'custom_check_iterations': custom_check_iterations + 1
    }


# Передает найденные аномалии и custom SQL-результаты агенту для финального ответа
async def final_anomaly_answer(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды final_anomaly_answer')

    findings = sort_findings(state['standard_findings'])
    client = get_openai_client()
    settings = get_settings()
    report_input = build_report_input(state, findings)

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=ANOMALY_REPORT_SYSTEM_PROMPT,
        input=report_input,
    )

    logger.info(f'Отчет по аномалиям сформирован')

    return {
        **state,
        'answer': response.output_text
    }
