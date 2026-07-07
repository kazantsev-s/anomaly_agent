from openai import AsyncOpenAI

from agent.state import AgentState
from config import get_settings

from logger import init_logger
logger = init_logger()

SYSTEM_PROMPT = (
    'Ты агент для анализа объявлений о продаже авто. Ты должен помогать искать аномалии. Отвечай на русском языке'
)


# Нода вызова openai
async def call_openai(state: AgentState) -> AgentState:
    logger.info(f'Вызов ноды call_openai с состоянием: {state}')
    settings = get_settings()

    if not settings.openai_api_key:
        logger.error('API ключ к OpenAI не задан в переменных окружения')
        raise ValueError('API ключ к OpenAI не задан в переменных окружения')

    # https://github.com/openai/openai-python#async-usage
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # https://developers.openai.com/api/reference/python/resources/responses/methods/create
    response = await client.responses.create(
        model=settings.openai_model,
        instructions=SYSTEM_PROMPT,
        input=state['prompt'],
    )
    logger.info(f'AI-агент вернул ответ: {response.output_text}')

    return {
        'prompt': state['prompt'],
        'answer': response.output_text
    }
