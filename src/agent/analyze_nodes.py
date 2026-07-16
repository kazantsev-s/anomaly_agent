from collections import Counter
from html import escape
import json

from agent.helpers import get_openai_client, load_prompt, render_prompt, strip_markdown_code_block
from agent.state import AnalyzeAgentState
from agent.tools import get_schema, profile_table, run_custom_sql, run_standard_checks
from config import get_settings
from db.postgres import (
    finish_analysis_run,
    get_evaluation_context,
    get_test_id_by_table_name,
    save_agent_findings,
    save_analysis_profile,
    save_analysis_run,
    save_evaluation_result,
)
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
    'non_positive_numeric_value': 'нулевые значения',
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
    'electric_car_positive_engine_volume': 'электромобиль с ненулевым объемом двигателя',
    'non_electric_car_zero_engine_volume': 'неэлектрический автомобиль с нулевым объемом двигателя',
}


ANOMALY_REPORT_SYSTEM_PROMPT = load_prompt('analyze/anomaly_report_system.prompt.md')
CUSTOM_CHECK_PLANNER_SYSTEM_PROMPT_TEMPLATE = load_prompt('analyze/custom_check_planner_system.prompt.md')
EVALUATION_MATCHER_SYSTEM_PROMPT = load_prompt('analyze/evaluation_matcher_system.prompt.md')


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


def build_custom_sql_findings(state: AnalyzeAgentState) -> list[dict]:
    # Результаты поиска кандидатов в аномалии сохраняем как findings для оценки качества
    findings = []

    for result in state['custom_sql_results']:
        if result['result_kind'] != 'anomaly_candidates' or result['error'] or not result['rows']:
            continue

        findings.append({
            'type': 'custom_sql_anomaly',
            'table': state['table_name'],
            'column': None,
            'importance': 'medium',
            'count': len(result['rows']),
            'reason': result['purpose'],
            'sample_rows': result['rows'],
            'details': {
                'name': result['name'],
                'result_kind': result['result_kind'],
                'query': result['query'],
            },
        })

    return findings


def build_evaluation_input(context: dict) -> str:
    # В matcher передаем сжатые признаки, чтобы не упереться в context window
    expected_anomalies = []
    agent_findings = []
    expected_kolesa_ids = {
        anomaly['kolesa_id']
        for anomaly in context['expected_anomalies']
    }
    expected_row_ids = {
        anomaly['source_row_number']
        for anomaly in context['expected_anomalies']
    }

    for anomaly in context['expected_anomalies']:
        expected_anomalies.append({
            'id': anomaly['id'],
            'anomaly_key': anomaly['anomaly_key'],
            'source_row_number': anomaly['source_row_number'],
            'kolesa_id': anomaly['kolesa_id'],
            'anomaly_type': anomaly['anomaly_type'],
            'changed_columns': anomaly['changed_columns'],
            'row_selector': anomaly['row_selector'],
            'expected_detection': anomaly['expected_detection'],
            'description': anomaly['description'],
        })

    candidate_findings = [
        finding
        for finding in context['agent_findings']
        if finding['kolesa_id'] in expected_kolesa_ids or finding['row_id'] in expected_row_ids
    ]

    for finding in candidate_findings:
        evidence = finding.get('evidence') or {}
        sample_row = evidence.get('sample_row')

        if sample_row:
            sample_row = {
                key: sample_row.get(key)
                for key in [
                    'id',
                    'kolesa_id',
                    'brand',
                    'model',
                    'year',
                    'body_type',
                    'fuel_type',
                    'engine_volume',
                    'mileage',
                    'drive_type',
                    'price',
                    'kolesa_url',
                ]
                if key in sample_row
            }

        agent_findings.append({
            'id': finding['id'],
            'finding_key': finding['finding_key'],
            'column_name': finding['column_name'],
            'anomaly_type': finding['anomaly_type'],
            'importance': finding['importance'],
            'row_id': finding['row_id'],
            'kolesa_id': finding['kolesa_id'],
            'description': finding['description'],
            'reason': evidence.get('reason'),
            'sample_row': sample_row,
        })

    payload = {
        'run': {
            'id': context['run']['id'],
            'test_id': context['run']['test_id'],
            'table_name': context['run']['table_name'],
        },
        'expected_anomalies': expected_anomalies,
        'agent_findings': agent_findings,
        'final_answer': context['run'].get('final_answer') or '',
    }
    logger.info(
        f'В оценку передано findings: {len(candidate_findings)} '
        f'из {len(context["agent_findings"])}'
    )
    return json.dumps(payload, ensure_ascii=False, default=str)


def prepare_evaluation_data(context: dict, matcher_result: dict, settings) -> dict:
    matcher_matches = matcher_result.get('matches')

    if not isinstance(matcher_matches, list):
        raise ValueError('Оценщик вернул JSON без списка matches')

    expected_by_id = {row['id']: row for row in context['expected_anomalies']}
    finding_by_id = {row['id']: row for row in context['agent_findings']}
    expected_ids = set(expected_by_id)
    finding_ids = set(finding_by_id)
    matches = []
    matched_finding_ids = set()
    seen_expected_ids = set()

    for match in matcher_matches:
        if not isinstance(match, dict):
            raise ValueError('Оценщик вернул некорректный элемент в matches')

        expected_id = match.get('expected_anomaly_id')
        finding_id = match.get('agent_finding_id')
        match_status = match.get('match_status')
        report_match_status = match.get('report_match_status')

        if expected_id not in expected_ids or expected_id in seen_expected_ids:
            continue

        seen_expected_ids.add(expected_id)

        expected_anomaly = expected_by_id[expected_id]
        finding = finding_by_id.get(finding_id)
        same_row = finding and (
            finding['kolesa_id'] == expected_anomaly['kolesa_id']
            or finding['row_id'] == expected_anomaly['source_row_number']
        )

        if match_status == 'matched' and finding_id in finding_ids and same_row and finding_id not in matched_finding_ids:
            matched_finding_ids.add(finding_id)
            matches.append({
                'expected_anomaly_id': expected_id,
                'agent_finding_id': finding_id,
                'match_status': 'matched',
                'confidence': match.get('confidence'),
                'matcher_reason': match.get('matcher_reason'),
                'report_match_status': 'visible' if report_match_status == 'visible' else 'not_visible',
                'report_matcher_reason': match.get('report_matcher_reason'),
            })
        else:
            matches.append({
                'expected_anomaly_id': expected_id,
                'agent_finding_id': None,
                'match_status': 'missed',
                'confidence': None,
                'matcher_reason': match.get('matcher_reason'),
                'report_match_status': 'visible' if report_match_status == 'visible' else 'not_visible',
                'report_matcher_reason': match.get('report_matcher_reason'),
            })

    matched_expected_ids = {match['expected_anomaly_id'] for match in matches}
    for expected_id in sorted(expected_ids - matched_expected_ids):
        matches.append({
            'expected_anomaly_id': expected_id,
            'agent_finding_id': None,
            'match_status': 'missed',
            'confidence': None,
            'matcher_reason': 'Оценщик не вернул match для этой ожидаемой аномалии',
            'report_match_status': 'not_visible',
            'report_matcher_reason': 'Оценщик не вернул результат проверки отчета',
        })

    extra_finding_ids = sorted(finding_ids - matched_finding_ids)
    expected_total = len(context['expected_anomalies'])
    expected_found = sum(1 for match in matches if match['match_status'] == 'matched')
    report_expected_found = sum(1 for match in matches if match['report_match_status'] == 'visible')

    return {
        'matcher_name': 'llm_expected_finding_matcher',
        'matcher_model_provider': 'openai',
        'matcher_model_name': settings.openai_model,
        'expected_total': expected_total,
        'expected_found': expected_found,
        'expected_recall': expected_found / expected_total if expected_total else 0,
        'agent_findings_count': len(context['agent_findings']),
        'matched_findings_count': len(matched_finding_ids),
        'missed_count': expected_total - expected_found,
        'extra_findings_count': len(extra_finding_ids),
        'report_expected_found': report_expected_found,
        'report_expected_recall': report_expected_found / expected_total if expected_total else 0,
        'report_missed_count': expected_total - report_expected_found,
        'matches': matches,
        'extra_finding_ids': extra_finding_ids,
        'raw_matches': matcher_result,
        'summary': matcher_result.get('search_summary') or matcher_result.get('summary', ''),
        'report_summary': matcher_result.get('report_summary', ''),
    }


def format_evaluation_answer(evaluation_data: dict) -> str:
    recall_percent = round(evaluation_data['expected_recall'] * 100, 1)
    report_recall_percent = round(evaluation_data['report_expected_recall'] * 100, 1)

    return (
        '<b>Оценка качества поиска</b>\n\n'
        f"Ожидалось аномалий: <code>{evaluation_data['expected_total']}</code>\n"
        f"Найдено ожидаемых: <code>{evaluation_data['expected_found']}</code>\n"
        f"Recall поиска: <code>{recall_percent}%</code>\n"
        f"Всего findings агента: <code>{evaluation_data['agent_findings_count']}</code>\n"
        f"Findings вне тестовой разметки, не участвующие в recall: <code>{evaluation_data['extra_findings_count']}</code>\n"
        f"Пропущено: <code>{evaluation_data['missed_count']}</code>\n\n"
        f"{escape(evaluation_data['summary'])}\n\n"
        '<b>Качество итогового отчета</b>\n\n'
        f"Показано ожидаемых аномалий: <code>{evaluation_data['report_expected_found']}</code>\n"
        f"Recall отчета: <code>{report_recall_percent}%</code>\n"
        f"Не показано в отчете: <code>{evaluation_data['report_missed_count']}</code>\n\n"
        f"{escape(evaluation_data['report_summary'])}"
    )


async def mark_analysis_run_failed(state: AnalyzeAgentState, error: Exception):
    # Если проход уже создан, фиксируем падение в БД и не скрываем исходную ошибку
    run_id = state.get('run_id')

    if not run_id:
        return

    try:
        await finish_analysis_run(
            run_id,
            'failed',
            {
                'standard_findings_count': len(state.get('standard_findings', [])),
                'custom_sql_count': state.get('custom_sql_count', 0),
                'custom_check_iterations': state.get('custom_check_iterations', 0),
                'custom_sql_results': state.get('custom_sql_results', []),
            },
            str(error),
        )
    except Exception:
        logger.exception('Не удалось отметить проход анализа как failed')


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
    # После профилирования создаем run, к которому дальше привяжутся findings
    test_id = await get_test_id_by_table_name(table_name)
    profile_id = await save_analysis_profile(table_name, state['schema'], profile)
    settings = get_settings()
    run_id = await save_analysis_run({
        'test_id': test_id,
        'profile_id': profile_id,
        'table_name': table_name,
        'agent_name': 'analyze_graph',
        'model_provider': 'openai',
        'model_name': settings.openai_model,
        'prompt_version': 'analyze_graph_v1',
        'metadata': {
            'custom_sql_total_limit': settings.analyze_custom_sql_total_limit,
            'custom_sql_per_iteration_limit': settings.analyze_custom_sql_per_iteration_limit,
            'max_custom_check_iterations': settings.analyze_max_custom_check_iterations,
        },
    })
    logger.info(f'Профиль таблицы {table_name} собран')
    logger.info(f'Проход анализа создан: run_id={run_id}, profile_id={profile_id}')

    return {
        **state,
        'test_id': test_id,
        'profile_id': profile_id,
        'run_id': run_id,
        'profile': profile
    }


# Запускает базовые SQL-проверки и инициализирует счетчики кастомных проверок
async def run_standard_checks_node(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды run_standard_checks_node')

    try:
        # Ошибка после создания run_id должна остаться в истории запусков
        standard_findings = await run_standard_checks(state['schema'], state['profile'])
    except Exception as e:
        await mark_analysis_run_failed(state, e)
        raise

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

    try:
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

                for field_name in ['name', 'purpose', 'result_kind', 'query']:
                    if field_name not in sql_query:
                        raise ValueError(f'В custom SQL-плане нет поля {field_name}')

                if sql_query['result_kind'] not in ['anomaly_candidates', 'context']:
                    raise ValueError('Агент вернул неизвестный тип результата custom SQL')

        logger.info(f'Агент предложил custom SQL-запросов: {len(sql_queries)}')

        return {
            **state,
            'custom_check_plan': custom_check_plan
        }
    except Exception as e:
        await mark_analysis_run_failed(state, e)
        raise


# Выполняет SQL-запросы, которые предложил агент, и сохраняет результаты
async def run_custom_checks(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды run_custom_checks')

    try:
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
            result_kind = sql_query['result_kind']
            query = sql_query['query']

            logger.info(f'Выполнение custom SQL-запроса {name}: {query}')

            if query in executed_queries:
                logger.info(f'Custom SQL-запрос {name} уже выполнялся')

                custom_sql_results.append({
                    'name': name,
                    'purpose': sql_query['purpose'],
                    'result_kind': result_kind,
                    'query': query,
                    'rows': [],
                    'error': 'Запрос уже выполнялся',
                })
                custom_sql_count += 1
                continue

            try:
                rows = await run_custom_sql(query, state['table_name'])
                executed_queries.add(query)
                custom_sql_results.append({
                    'name': name,
                    'purpose': sql_query['purpose'],
                    'result_kind': result_kind,
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
                    'result_kind': result_kind,
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
    except Exception as e:
        await mark_analysis_run_failed(state, e)
        raise


# Передает найденные аномалии и custom SQL-результаты агенту для финального ответа
async def final_anomaly_answer(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды final_anomaly_answer')

    findings = sort_findings(state['standard_findings'])
    all_findings = findings + build_custom_sql_findings(state)
    run_id = state.get('run_id')
    settings = get_settings()
    findings_saved_count = 0

    try:
        # Сохраняем findings до LLM-отчета, чтобы даже ошибка отчета не потеряла результаты проверок
        if run_id:
            findings_saved_count = await save_agent_findings(run_id, all_findings)

        client = get_openai_client()
        report_input = build_report_input(state, findings)

        response = await client.responses.create(
            model=settings.openai_model,
            instructions=ANOMALY_REPORT_SYSTEM_PROMPT,
            input=report_input,
        )

        if run_id:
            # В metadata оставляем данные, которые не требуют отдельной таблицы
            await finish_analysis_run(
                run_id,
                'completed',
                {
                    'standard_findings_count': len(findings),
                    'custom_sql_findings_count': len(all_findings) - len(findings),
                    'saved_agent_findings_count': findings_saved_count,
                    'custom_sql_count': state['custom_sql_count'],
                    'custom_check_iterations': state['custom_check_iterations'],
                    'custom_sql_results': state['custom_sql_results'],
                    'final_answer': response.output_text,
                },
            )
    except Exception as e:
        if run_id:
            # Если финальный ответ не собрался, run все равно закрывается как failed
            await finish_analysis_run(
                run_id,
                'failed',
                {
                    'standard_findings_count': len(findings),
                    'custom_sql_findings_count': len(all_findings) - len(findings),
                    'saved_agent_findings_count': findings_saved_count,
                    'custom_sql_count': state.get('custom_sql_count', 0),
                    'custom_check_iterations': state.get('custom_check_iterations', 0),
                    'custom_sql_results': state.get('custom_sql_results', []),
                },
                str(e),
            )
        raise

    logger.info(f'Отчет по аномалиям сформирован')

    return {
        **state,
        'answer': response.output_text
    }


# Сравнивает expected_anomalies и agent_findings после финального отчета
async def evaluate_analysis(state: AnalyzeAgentState) -> AnalyzeAgentState:
    logger.info(f'Вызов ноды evaluate_analysis')

    run_id = state.get('run_id')
    settings = get_settings()

    if not run_id or not state.get('test_id'):
        return state

    try:
        context = await get_evaluation_context(run_id)
        if not context:
            return state

        client = get_openai_client()
        response = await client.responses.create(
            model=settings.openai_model,
            instructions=EVALUATION_MATCHER_SYSTEM_PROMPT,
            input=build_evaluation_input(context),
        )
        matcher_result = parse_llm_json(response.output_text)
        evaluation_data = prepare_evaluation_data(context, matcher_result, settings)
        evaluation_result_id = await save_evaluation_result(
            run_id,
            context['run']['test_id'],
            evaluation_data,
        )
        await finish_analysis_run(
            run_id,
            'completed',
            {'evaluation_result_id': evaluation_result_id},
        )

        logger.info(f'Оценка анализа сохранена: evaluation_result_id={evaluation_result_id}')
        return {
            **state,
            'evaluation_result': evaluation_data,
            'evaluation_answer': format_evaluation_answer(evaluation_data),
        }
    except Exception as e:
        logger.exception('Ошибка оценки качества анализа')
        return {
            **state,
            'evaluation_answer': f'Не удалось рассчитать качество работы агента: {escape(str(e))}',
        }
