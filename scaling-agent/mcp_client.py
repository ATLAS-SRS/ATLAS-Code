import asyncio
import json
import os
import sys

from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from structured_logger import get_logger


LOGGER = get_logger("mcp-client")
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "").strip()
LLM_API_URL = os.getenv("LLM_API_URL", "").strip() or LM_STUDIO_URL or "http://host.docker.internal:1234/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
MAX_STEPS = int(os.getenv("MAX_TOOL_STEPS", "5"))
TARGET_DEPLOYMENTS = [
    deployment.strip()
    for deployment in os.getenv(
        "TARGET_DEPLOYMENTS",
        "api-gateway,scoring-system,enrichment-system,notification-system",
    ).split(",")
    if deployment.strip()
]

if not TARGET_DEPLOYMENTS:
    raise RuntimeError("TARGET_DEPLOYMENTS non può essere vuoto")


def _build_system_prompt(deployment: str) -> str:
    return f"""Sei un autoscaler autonomo di Kubernetes.
Il tuo obiettivo è mantenere stabile il deployment corrente: {deployment}.
I deployment target sono: {', '.join(TARGET_DEPLOYMENTS)}.
Regole operative:
1. Prima di decidere, usa get_scaling_recommendation per il deployment corrente.
2. Usa get_current_replicas per leggere lo stato attuale se necessario.
3. Se il traffico è basso e le repliche sono alte, DEVI usare set_replicas per fare SCALE DOWN.
4. Se il traffico è alto e le repliche sono poche, DEVI usare set_replicas per fare SCALE UP.
5. Se la situazione è stabile, non fare nulla (HOLD).
6. Non modificare deployment diversi da quello corrente.
Produci sempre un breve riassunto testuale alla fine di ogni tuo intervento."""


def _build_user_prompt(deployment: str) -> str:
    return (
        f"Analizza il deployment '{deployment}' e le sue metriche. "
        "Se la raccomandazione indica scaling, applicala con lo stesso criterio "
        "usato per gli altri servizi del flusso."
    )


def _tool_to_openai_schema(tool):
    """Converte un tool MCP nel formato richiesto dalle API OpenAI/LM Studio"""
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {})
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": getattr(tool, "description", ""),
            "parameters": schema,
        },
    }


async def _run_agent_cycle(session: ClientSession, llm_client: AsyncOpenAI, openai_tools: list, deployment: str):
    """Gestisce il ciclo di ragionamento e chiamate agli strumenti con l'LLM"""
    messages = [
        {"role": "system", "content": _build_system_prompt(deployment)},
        {"role": "user", "content": _build_user_prompt(deployment)},
    ]

    for step in range(MAX_STEPS):
        response = await llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            temperature=0.0,
        )

        assistant_msg = response.choices[0].message

        # Aggiungiamo la risposta del modello alla history (gestione sicura per pydantic/dizionari)
        msg_dict = assistant_msg.model_dump(exclude_unset=True)
        messages.append(msg_dict)

        # Se l'LLM non ha chiamato strumenti, ha preso la sua decisione finale
        if not assistant_msg.tool_calls:
            return assistant_msg.content

        # Esecuzione fisica dei Tools richiesti dall'LLM
        for tool_call in assistant_msg.tool_calls:
            tool_name = tool_call.function.name

            try:
                tool_args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_output = {
                    "ok": False,
                    "error": "Invalid JSON arguments provided. Please correct the format and try again.",
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_output),
                    }
                )
                continue

            try:
                LOGGER.info("Esecuzione Tool MCP", extra={"deployment": deployment, "tool": tool_name, "tool_args": tool_args})
                result = await session.call_tool(tool_name, arguments=tool_args)

                # MCP restituisce blocchi di testo o immagini, estraiamo il testo
                testo_risultato = " ".join([block.text for block in result.content if hasattr(block, "text")])
                tool_output = {"ok": True, "result": testo_risultato}

            except Exception as e:
                LOGGER.error("Errore esecuzione Tool", extra={"deployment": deployment, "tool": tool_name, "error": str(e)})
                tool_output = {"ok": False, "error": str(e)}

            # Restituiamo il risultato della funzione all'LLM
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_output),
                }
            )

    return "Raggiunto numero massimo di step. Operazione terminata per timeout."


async def main():
    # Normalizza l'URL per la libreria OpenAI
    base_url = LLM_API_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    llm_client = AsyncOpenAI(base_url=base_url, api_key="local-no-key")

    # Questo è il cuore di MCP locale: il Client esegue il file del Server come sottoprocesso!
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-u", "mcp_server.py"],
        env=os.environ.copy(),
    )

    LOGGER.info("Avvio MCP AIOps Controller. In attesa del primo ciclo...", extra={"target_deployments": TARGET_DEPLOYMENTS})

    while True:
        try:
            # Apriamo la connessione al server MCP
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    # 1. Scoperta dinamica dei Tools
                    listed_tools = await session.list_tools()
                    openai_tools = [_tool_to_openai_schema(t) for t in listed_tools.tools]

                    LOGGER.info(
                        "Inizio ciclo di analisi",
                        extra={
                            "tools_caricati": [t.name for t in listed_tools.tools],
                            "target_deployments": TARGET_DEPLOYMENTS,
                        },
                    )

                    # 2. Ragionamento AI ed esecuzione per ogni deployment target
                    for deployment in TARGET_DEPLOYMENTS:
                        report = await _run_agent_cycle(session, llm_client, openai_tools, deployment)

                        LOGGER.info("Ciclo concluso", extra={"deployment": deployment, "decisione_llm": report})

        except Exception as e:
            LOGGER.error("Errore fatale nel loop principale", extra={"error": str(e)})

        # Pausa prima del prossimo ciclo
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())