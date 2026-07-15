FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY listener.py .
COPY alerts/ ./alerts/


CMD ["python", "listener.py"]
