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

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]
