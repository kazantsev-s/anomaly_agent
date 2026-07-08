from typing import TypedDict

# Описание состояния/памяти агента
class AgentState(TypedDict):
    prompt: str
    sql_query: str
    sql_result: str
    answer: str
