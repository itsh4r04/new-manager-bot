# Minimal image with Python 3.12
FROM python:3.12-slim

# Ensure logs are not buffered
ENV PYTHONUNBUFFERED=1

# Default port (platforms override $PORT)
ENV PORT=8000

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# (Optional) expose default port for local runs
EXPOSE 8000

# Start the bot (long polling + tiny Flask health server)
CMD ["python", "bot.py"]
