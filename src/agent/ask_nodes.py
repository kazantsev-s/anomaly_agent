import json

from db.postgres import execute_sql_query
from agent.helpers import get_openai_client, load_prompt, render_prompt, strip_markdown_code_block
from agent.state import AskAgentState
from config import get_settings

from logger import init_logger
logger = init_logger()

KOLESA_FIELDS_DESCRIPTION = load_prompt('common/kolesa_fields.prompt.md')
SQL_SYSTEM_PROMPT = render_prompt(load_prompt('ask/sql_system.prompt.md'), fields_description=KOLESA_FIELDS_DESCRIPTION)
ANSWER_SYSTEM_PROMPT = load_prompt('ask/answer_system.prompt.md')
ANSWER_USER_PROMPT_TEMPLATE = load_prompt('ask/answer_user.prompt.md')

# Ноды графа

# Нода: превращает вопрос пользователя в запрос к таблице
async def generate_sql_query(state: AskAgentState) -> AskAgentState:
    logger.info(f'Вызов ноды generate_sql_query')
    client = get_openai_client()
    settings = get_settings()

    # https://developers.openai.com/api/reference/python/resources/responses/methods/create
    response = await client.responses.create(
        model=settings.openai_model,
        instructions=SQL_SYSTEM_PROMPT,
        input=state['prompt']
    )

    sql_query = strip_markdown_code_block(response.output_text, 'sql').rstrip(';').strip()
    logger.info(f'Агент сгенерировал SQL-запрос: {sql_query}')

    return {
        **state,
        'sql_query': sql_query
    }


# Нода: выполняет раннее сгенерированный SQL запрос и возвращает результат в JSON
async def execute_sql(state: AskAgentState) -> AskAgentState:
    sql_query = state['sql_query']

    if not sql_query:
        logger.error('SQL-запрос пустой')
        raise ValueError('SQL-запрос пустой')
    
    logger.info(f'Вызов ноды execute_sql запроса: {sql_query}')

    sql_result = await execute_sql_query(sql_query)
    sql_result_json = json.dumps(sql_result, ensure_ascii=False, default=str)

    logger.info(f'Результат выполнения SQL-запроса: {sql_result_json}')

    return {
        **state,
        'sql_result': sql_result_json,
    }


# Нода: формирует ответ пользователю на основе вопроса пользователя, выполненного запроса и результата
async def answer(state: AskAgentState) -> AskAgentState:
    logger.info(f'Вызов ноды answer')

    client = get_openai_client()
    settings = get_settings()
    user_prompt = render_prompt(
        ANSWER_USER_PROMPT_TEMPLATE,
        user_prompt=state['prompt'],
        sql_query=state['sql_query'],
        sql_result=state['sql_result'],
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
