FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    docker.io \
    docker-compose \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run as root for Docker socket access
# Note: In production, this should be properly secured

EXPOSE 8001
EXPOSE 8002

CMD ["python", "-u", "scaling_server.py"]
