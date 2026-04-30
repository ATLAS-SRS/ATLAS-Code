#!/usr/bin/env python3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from src.agent_chaos.config import LOGGER
from src.agent_chaos.graph import ChaosAgentRuntime


class TriggerChaosRequest(BaseModel):
    target_deployment: str | None = None
    objective: str | None = None


runtime = ChaosAgentRuntime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title="ATLAS Chaos Agent",
    description="Chaos engineering controller powered by LangGraph + MCP",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/trigger-chaos")
async def trigger_chaos(request: TriggerChaosRequest | None = None) -> dict[str, str]:
    request = request or TriggerChaosRequest()

    user_goal = (
        request.objective
        or "Execute one controlled chaos experiment following the 4 defined phases and finish with a formal Markdown report."
    )
    if request.target_deployment:
        user_goal = f"Target deployment: {request.target_deployment}.{user_goal}"

    initial_state = {
        "messages": [HumanMessage(content=user_goal)],
        "target_deployment": request.target_deployment or "",
        "chaos_plan": "",
        "execution_report": "",
    }

    try:
        final_state = await runtime.graph.ainvoke(initial_state)
    except Exception as exc:
        LOGGER.error("Chaos execution failed", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Chaos execution failed: {exc}") from exc

    execution_report = (final_state.get("execution_report") or "").strip()
    if not execution_report:
        execution_report = "No execution report was produced by the chaos workflow."

    # Recuperiamo il target effettivamente colpito dall'agente dallo stato finale
    actual_target = final_state.get("target_deployment") or request.target_deployment or "unknown"

    LOGGER.info(
        "Chaos experiment completed",
        extra={"target_deployment": actual_target},
    )

    return {
        "status": "success",
        "target_deployment": actual_target,
        "execution_report": execution_report,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main_chaos_agent:app", host="0.0.0.0", port=8001, log_level="info")
