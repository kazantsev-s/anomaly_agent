FROM python:3.12-slim

ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY db.sql .
COPY data ./data
COPY src ./src

CMD ["python", "-m", "bot.main"]
