FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY smarttrader /app/smarttrader
CMD ["python", "-B", "-m", "smarttrader.runner"]
