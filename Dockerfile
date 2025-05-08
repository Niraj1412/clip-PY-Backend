FROM python:3.12-alpine

WORKDIR /app

RUN apk update && \
    apk add --no-cache \
    ffmpeg \
    build-base \
    libffi-dev \
    && rm -rf /var/cache/apk/*

COPY requirements.txt . 

RUN pip install -r requirements.txt 

COPY . .

RUN mkdir -p /app/tmp && chmod 777 /app/tmp

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app:app"]
