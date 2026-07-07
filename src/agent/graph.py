from langgraph.graph import END, START, StateGraph

from agent.nodes import call_openai
from agent.state import AgentState

graph_builder = StateGraph(AgentState)

# Собираем граф агента (ноды и связи между ними)
graph_builder.add_node('call_openai', call_openai)
graph_builder.add_edge(START, 'call_openai')
graph_builder.add_edge('call_openai', END)

agent_graph = graph_builder.compile()
