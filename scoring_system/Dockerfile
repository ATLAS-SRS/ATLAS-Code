FROM python:3.11-slim

WORKDIR /workspace

COPY scoring_system/requirements.txt /tmp/scoring_requirements.txt
RUN pip install --no-cache-dir -r /tmp/scoring_requirements.txt

COPY . .

ENV PYTHONPATH=/workspace

CMD ["python", "-m", "scoring_system.scoring"]
