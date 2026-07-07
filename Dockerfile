FROM python:3.12-slim

ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY table.sql .
COPY data/kolesa.csv ./data/kolesa.csv
COPY src ./src

CMD ["python", "-m", "bot.main"]
