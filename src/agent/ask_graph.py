from langgraph.graph import END, START, StateGraph

from agent.ask_nodes import answer, execute_sql, generate_sql_queries, route_after_execute_sql
from agent.state import AskAgentState

graph_builder = StateGraph(AskAgentState)

# Собираем граф агента (ноды и связи между ними)

# Инициализация нод
graph_builder.add_node('generate_sql_queries', generate_sql_queries)
graph_builder.add_node('execute_sql', execute_sql)
graph_builder.add_node('answer', answer)

# Связи между нодами
graph_builder.add_edge(START, 'generate_sql_queries')
graph_builder.add_edge('generate_sql_queries', 'execute_sql')
graph_builder.add_conditional_edges(
    'execute_sql',
    route_after_execute_sql,
    {
        'execute_sql': 'execute_sql',
        'answer': 'answer',
    },
)
graph_builder.add_edge('answer', END)

ask_graph = graph_builder.compile()
