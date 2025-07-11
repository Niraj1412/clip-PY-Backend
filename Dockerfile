FROM python:3.12-alpine

WORKDIR /app

# Add edge repositories for latest Chromium and update package list
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

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for downloads and temp files
ENV DOWNLOAD_DIR=/app/downloads
ENV TMP_DIR=/app/temp
RUN mkdir -p $DOWNLOAD_DIR $TMP_DIR

# Set executable permissions if needed (optional)
RUN chmod +x /usr/bin/chromium-browser /usr/bin/chromedriver

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]