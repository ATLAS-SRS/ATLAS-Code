# Usa un'immagine base leggera ma aggiornata
FROM python:3.11-slim

# Evita la scrittura di file .pyc e forza l'output dei log senza buffer
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Installa le dipendenze di sistema necessarie per confluent-kafka
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

# Copia i requirements e installa le dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutto il codice sorgente (main.py, structured_logger.py, ecc.)
COPY . .

# Esponi la porta esatta su cui si aspetta di parlare Kubernetes/NGINX
EXPOSE 8080

# Comando di avvio per FastAPI usando Uvicorn
CMD ["uvicorn", "notification_system:app", "--host", "0.0.0.0", "--port", "8080"]