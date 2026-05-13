FROM gradle:8.7-jdk17 AS tvbox-jar-build

WORKDIR /build/tvbox-jar

COPY tvbox-jar/settings.gradle /build/tvbox-jar/settings.gradle
COPY tvbox-jar/build.gradle /build/tvbox-jar/build.gradle
COPY tvbox-jar/src /build/tvbox-jar/src

RUN gradle --no-daemon clean jar

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
COPY --from=tvbox-jar-build /build/tvbox-jar/build/libs/colvins-tvbox-spider-0.1.0.jar /app/app/artifacts/colvins-tvbox-spider.jar
RUN mkdir -p /app/data /app/app/artifacts

EXPOSE 8788

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
