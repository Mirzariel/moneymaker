FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
VOLUME ["/app/data"]
CMD ["moneymaker", "run"]
# .env and config.yaml must exist on the host before `docker compose up` (run start.sh once, or copy the examples).
