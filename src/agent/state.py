from typing import TypedDict

# Описание состояния/памяти агента
class AgentState(TypedDict):
    prompt: str
    answer: str
