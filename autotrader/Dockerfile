FROM python:3.12-slim
WORKDIR /app
COPY autotrader /app/autotrader
ENV PYTHONUNBUFFERED=1 CAP_ENV=demo BOT_MODE=paper
CMD ["python", "-m", "autotrader.bot"]
