from typing import Any, TypedDict

# Описание состояния/памяти агента
class AgentState(TypedDict):
    prompt: str
    sql_query: str
    sql_result: str
    answer: str


class AnomalyAgentState(TypedDict, total=False):
    table_name: str
    schema: dict[str, Any]
    profile: dict[str, Any]
    standard_findings: list[dict[str, Any]]
    all_findings: list[dict[str, Any]]
    answer: str
