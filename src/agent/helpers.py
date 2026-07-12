from pathlib import Path
from string import Template

from openai import AsyncOpenAI

from config import get_settings
from logger import init_logger


PROMPTS_DIR = Path(__file__).parent / 'prompts'


def load_prompt(relative_path: str) -> str:
    return (PROMPTS_DIR / relative_path).read_text(encoding='utf-8').strip()


def render_prompt(prompt: str, **variables) -> str:
    template_variables = {
        key: str(value)
        for key, value in variables.items()
    }
    return Template(prompt).substitute(template_variables)


def strip_markdown_code_block(text: str, language: str) -> str:
    result = text.strip()
    language_prefix = f'```{language}'

    if result.startswith(language_prefix):
        result = result.removeprefix(language_prefix).strip()

    if result.startswith('```'):
        result = result.removeprefix('```').strip()

    if result.endswith('```'):
        result = result.removesuffix('```').strip()

    return result


def get_openai_client() -> AsyncOpenAI:
    logger = init_logger()
    logger.info('Инициализация OpenAI-клиента')
    settings = get_settings()

    if not settings.openai_api_key:
        logger.error('API ключ к OpenAI не задан в переменных окружения')
        raise ValueError('API ключ к OpenAI не задан в переменных окружения')

    return AsyncOpenAI(api_key=settings.openai_api_key)
