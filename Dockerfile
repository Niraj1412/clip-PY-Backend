FROM python:3.12-alpine

WORKDIR /app

# Add edge repositories for Chromium and update package list
RUN echo "http://dl-cdn.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    && echo "http://dl-cdn.alpinelinux.org/alpine/edge/main" >> /etc/apk/repositories \
    && apk update && apk add --no-cache \
    ffmpeg \
    build-base \
    libffi-dev \
    curl \
    chromium \
    chromium-chromedriver \
    && rm -rf /var/cache/apk/*

COPY requirements54.txt requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional: create download/temp folders
ENV DOWNLOAD_DIR=/app/downloads
ENV TMP_DIR=/app/temp

RUN mkdir -p $DOWNLOAD_DIR $TMP_DIR

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]