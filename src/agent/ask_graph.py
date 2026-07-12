from langgraph.graph import END, START, StateGraph

from agent.ask_nodes import generate_sql_query, execute_sql, answer
from agent.state import AskAgentState

graph_builder = StateGraph(AskAgentState)

# Собираем граф агента (ноды и связи между ними)

# Инициализация нод
graph_builder.add_node('generate_sql_query', generate_sql_query)
graph_builder.add_node('execute_sql', execute_sql)
graph_builder.add_node('answer', answer)

# Связи между нодами
graph_builder.add_edge(START, 'generate_sql_query')
graph_builder.add_edge('generate_sql_query', 'execute_sql')
graph_builder.add_edge('execute_sql', 'answer')
graph_builder.add_edge('answer', END)

ask_graph = graph_builder.compile()
