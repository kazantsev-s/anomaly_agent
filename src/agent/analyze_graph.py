from langgraph.graph import END, START, StateGraph

from agent.analyze_nodes import (
    evaluate_analysis,
    final_anomaly_answer,
    load_schema,
    plan_custom_checks,
    profile_table_node,
    route_after_plan,
    run_custom_checks,
    run_standard_checks_node,
)
from agent.state import AnalyzeAgentState


analyze_graph_builder = StateGraph(AnalyzeAgentState)

analyze_graph_builder.add_node('load_schema', load_schema)
analyze_graph_builder.add_node('profile_table', profile_table_node)
analyze_graph_builder.add_node('run_standard_checks', run_standard_checks_node)
analyze_graph_builder.add_node('plan_custom_checks', plan_custom_checks)
analyze_graph_builder.add_node('run_custom_checks', run_custom_checks)
analyze_graph_builder.add_node('final_anomaly_answer', final_anomaly_answer)
analyze_graph_builder.add_node('evaluate_analysis', evaluate_analysis)

analyze_graph_builder.add_edge(START, 'load_schema')
analyze_graph_builder.add_edge('load_schema', 'profile_table')
analyze_graph_builder.add_edge('profile_table', 'run_standard_checks')
analyze_graph_builder.add_edge('run_standard_checks', 'plan_custom_checks')
analyze_graph_builder.add_conditional_edges(
    'plan_custom_checks',
    route_after_plan,
    {
        'run_custom_checks': 'run_custom_checks',
        'final_anomaly_answer': 'final_anomaly_answer',
    },
)
analyze_graph_builder.add_edge('run_custom_checks', 'plan_custom_checks')
analyze_graph_builder.add_edge('final_anomaly_answer', 'evaluate_analysis')
analyze_graph_builder.add_edge('evaluate_analysis', END)

analyze_graph = analyze_graph_builder.compile()
