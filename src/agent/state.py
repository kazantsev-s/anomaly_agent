from typing import Any, TypedDict

# Описание состояния/памяти агента
class AskAgentState(TypedDict, total=False):
    prompt: str
    sql_query: str
    sql_result: str
    answer: str


class AnalyzeAgentState(TypedDict, total=False):
    table_name: str
    test_id: str | None
    profile_id: int
    run_id: int
    schema: dict[str, Any]
    profile: dict[str, Any]
    standard_findings: list[dict[str, Any]]
    custom_check_plan: dict[str, Any]
    custom_sql_results: list[dict[str, Any]]
    custom_sql_count: int
    custom_check_iterations: int
    answer: str
    evaluation_answer: str
    evaluation_result: dict[str, Any]
