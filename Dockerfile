FROM python:3.12-slim

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .
COPY templates/ templates/
RUN mkdir -p data

# Порт веб-панели (Render/Railway подставляют свой PORT)
ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
