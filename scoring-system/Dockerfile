FROM python:3.11-slim

WORKDIR /app

COPY scoring-system/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY scoring-system/ .

ENV PYTHONPATH=/app

CMD ["python", "-m", "scoring"]
