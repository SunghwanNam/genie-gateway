FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=38085

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 gateway \
    && chown -R gateway:gateway /app
USER gateway

EXPOSE 38085

CMD ["uvicorn", "app.main:app"]
