from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ChaosAgentState(TypedDict):
    messages: Annotated[list, add_messages]
    target_deployment: str
    chaos_plan: str
    execution_report: str
