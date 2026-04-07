FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir kubernetes openai mcp fastmcp requests

COPY mcp_server.py mcp_client.py structured_logger.py ./

CMD ["python", "-u", "mcp_client.py"]
