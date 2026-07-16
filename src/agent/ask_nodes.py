import json

from db.postgres import execute_sql_query
from agent.helpers import get_openai_client, load_prompt, render_prompt, strip_markdown_code_block
from agent.state import AskAgentState
from config import get_settings

from logger import init_logger
logger = init_logger()

KOLESA_FIELDS_DESCRIPTION = load_prompt('common/kolesa_fields.prompt.md')
SQL_SYSTEM_PROMPT_TEMPLATE = load_prompt('ask/sql_system.prompt.md')
ANSWER_SYSTEM_PROMPT = load_prompt('ask/answer_system.prompt.md')
ANSWER_USER_PROMPT_TEMPLATE = load_prompt('ask/answer_user.prompt.md')


# Вспомогательные функции

# Парсит план SQL-запросов из ответа агента
def parse_sql_plan(raw_text: str, max_sql_queries: int) -> list[dict]:
    text = strip_markdown_code_block(raw_text, 'json')

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError('Агент вернул невалидный JSON с планом SQL-запросов') from e

    if not isinstance(result, dict):
        raise ValueError('Агент вернул JSON не в формате объекта')

    sql_queries = result.get('sql_queries')

    if not isinstance(sql_queries, list) or not sql_queries:
        raise ValueError('Агент вернул пустой план SQL-запросов')

    sql_queries = sql_queries[:max_sql_queries]

    for sql_query in sql_queries:
        if not isinstance(sql_query, dict):
            raise ValueError('SQL-запрос в плане должен быть объектом')

        for field_name in ['name', 'purpose', 'query']:
            if field_name not in sql_query:
                raise ValueError(f'В плане SQL-запросов нет поля {field_name}')

        sql_query['query'] = sql_query['query'].rstrip(';').strip()

        if not sql_query['query']:
            raise ValueError('SQL-запрос пустой')

    return sql_queries


# Роутинг ask_graph после выполнения SQL-запроса
def route_after_execute_sql(state: AskAgentState) -> str:
    if state['current_sql_query_index'] < len(state['sql_queries']):
        return 'execute_sql'

    return 'answer'


# Ноды графа

# Нода: превращает вопрос пользователя в план запросов к таблице
async def generate_sql_queries(state: AskAgentState) -> AskAgentState:
    logger.info(f'Вызов ноды generate_sql_queries')
    client = get_openai_client()
    settings = get_settings()
    sql_system_prompt = render_prompt(
        SQL_SYSTEM_PROMPT_TEMPLATE,
        fields_description=KOLESA_FIELDS_DESCRIPTION,
        max_sql_queries=settings.ask_sql_query_limit,
    )

    # https://developers.openai.com/api/reference/python/resources/responses/methods/create
    response = await client.responses.create(
        model=settings.openai_model,
        instructions=sql_system_prompt,
        input=state['prompt']
    )

    sql_queries = parse_sql_plan(response.output_text, settings.ask_sql_query_limit)
    logger.info(f'Агент сгенерировал SQL-запросов: {len(sql_queries)}')

    return {
        **state,
        'sql_queries': sql_queries,
        'sql_results': [],
        'current_sql_query_index': 0,
    }


# Нода: выполняет следующий SQL-запрос из плана
async def execute_sql(state: AskAgentState) -> AskAgentState:
    sql_query_index = state['current_sql_query_index']
    sql_query = state['sql_queries'][sql_query_index]
    query = sql_query['query']

    logger.info(f'Вызов ноды execute_sql запроса {sql_query["name"]}: {query}')

    sql_result = await execute_sql_query(query)
    sql_result_json = json.dumps(sql_result, ensure_ascii=False, default=str)
    sql_results = list(state['sql_results'])
    sql_results.append({
        'name': sql_query['name'],
        'purpose': sql_query['purpose'],
        'query': query,
        'rows': sql_result,
    })

    logger.info(f'Результат выполнения SQL-запроса: {sql_result_json}')

    return {
        **state,
        'sql_results': sql_results,
        'current_sql_query_index': sql_query_index + 1,
    }


# Нода: формирует ответ пользователю на основе вопроса и результатов SQL-запросов
async def answer(state: AskAgentState) -> AskAgentState:
    logger.info(f'Вызов ноды answer')

    client = get_openai_client()
    settings = get_settings()
    sql_results_json = json.dumps(state['sql_results'], ensure_ascii=False, default=str)
    user_prompt = render_prompt(
        ANSWER_USER_PROMPT_TEMPLATE,
        user_prompt=state['prompt'],
        sql_results=sql_results_json,
    )

    response = await client.responses.create(
        model=settings.openai_model,
        instructions=ANSWER_SYSTEM_PROMPT,
        input=user_prompt,
    )

    logger.info(f'Агент вернул ответ: {response.output_text}')

    return {
        **state,
        'answer': response.output_text
    }
