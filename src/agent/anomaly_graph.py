from langgraph.graph import END, START, StateGraph

from agent.anomaly_nodes import (
    final_anomaly_answer,
    load_schema,
    merge_findings,
    plan_custom_checks,
    profile_table_node,
    route_after_plan,
    run_custom_checks,
    run_standard_checks_node,
)
from agent.state import AnomalyAgentState


anomaly_graph_builder = StateGraph(AnomalyAgentState)

anomaly_graph_builder.add_node('load_schema', load_schema)
anomaly_graph_builder.add_node('profile_table', profile_table_node)
anomaly_graph_builder.add_node('run_standard_checks', run_standard_checks_node)
anomaly_graph_builder.add_node('merge_findings', merge_findings)
anomaly_graph_builder.add_node('plan_custom_checks', plan_custom_checks)
anomaly_graph_builder.add_node('run_custom_checks', run_custom_checks)
anomaly_graph_builder.add_node('final_anomaly_answer', final_anomaly_answer)

anomaly_graph_builder.add_edge(START, 'load_schema')
anomaly_graph_builder.add_edge('load_schema', 'profile_table')
anomaly_graph_builder.add_edge('profile_table', 'run_standard_checks')
anomaly_graph_builder.add_edge('run_standard_checks', 'merge_findings')
anomaly_graph_builder.add_edge('merge_findings', 'plan_custom_checks')
anomaly_graph_builder.add_conditional_edges(
    'plan_custom_checks',
    route_after_plan,
    {
        'run_custom_checks': 'run_custom_checks',
        'final_anomaly_answer': 'final_anomaly_answer',
    },
)
anomaly_graph_builder.add_edge('run_custom_checks', 'plan_custom_checks')
anomaly_graph_builder.add_edge('final_anomaly_answer', END)

anomaly_agent_graph = anomaly_graph_builder.compile()
