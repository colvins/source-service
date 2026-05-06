FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN npm install --global axios@1.9.0 cheerio@1.1.0

COPY app /app/app
RUN mkdir -p /app/data

EXPOSE 8788

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
