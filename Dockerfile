FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && \
    playwright install --with-deps chromium

COPY . .
CMD ["python", "bot.py"]
