FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CHAT_DATA_DIR=/data
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home chat && mkdir /data && chown chat:chat /data
COPY --chown=chat:chat . /app/realtime
USER chat
EXPOSE 8000
CMD ["uvicorn", "realtime.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
