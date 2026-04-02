FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py database.py gpx_processor.py gemini_insights.py ./

# Database lives in a volume so it survives container restarts
VOLUME ["/app/data"]

ENV DB_PATH=/app/data/leaderboard.db

CMD ["python", "bot.py"]
