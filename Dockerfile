FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create logs directory
RUN mkdir -p logs

# Expose the health server port (Render sets PORT env var)
EXPOSE 10000

# Run the bot
CMD ["python", "-u", "main.py"]
