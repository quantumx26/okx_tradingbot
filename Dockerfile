FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY binance_webhook_server.py .

# Expose port
EXPOSE 8080

# Run the application
CMD ["gunicorn", "binance_webhook_server:app", "--bind", "0.0.0.0:8080", "--workers", "1"]
