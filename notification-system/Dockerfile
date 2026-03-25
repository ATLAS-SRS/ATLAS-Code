FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project structure to access the notification service
COPY . .

ENV PYTHONUNBUFFERED=1

# Run the notification service
CMD ["python", "notification_broker.py"]
