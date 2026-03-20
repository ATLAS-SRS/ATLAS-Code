# Usa un'immagine Python ufficiale e leggera come base
FROM python:3.11-slim

# Imposta la directory di lavoro nel container
WORKDIR /app

# Copia i requirements e installa le dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del codice
COPY client.py .

# Comando di default per avviare lo script
CMD ["python", "client.py"]