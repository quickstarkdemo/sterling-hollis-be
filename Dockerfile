FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

COPY . .
RUN chmod +x /app/scripts/run_api.sh

EXPOSE 8000
CMD ["./scripts/run_api.sh"]
