FROM python:3-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN apt-get update && \
    BUILD_DEPS="git" && \
    apt-get install -y --no-install-recommends $BUILD_DEPS && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove $BUILD_DEPS && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
COPY . /app
CMD ["python", "app.py"]
