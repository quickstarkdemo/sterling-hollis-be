FROM python:3.11-slim

ARG DD_GIT_REPOSITORY_URL
ARG DD_GIT_COMMIT_SHA

WORKDIR /app
ENV PYTHONPATH=/app \
    DD_MAIN_PACKAGE=app \
    DD_GIT_REPOSITORY_URL=${DD_GIT_REPOSITORY_URL} \
    DD_GIT_COMMIT_SHA=${DD_GIT_COMMIT_SHA}

COPY pyproject.toml README.md setup.py ./
COPY app ./app
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

COPY . .
RUN chmod +x /app/scripts/run_api.sh
RUN chmod +x /app/scripts/run_index_worker.sh

EXPOSE 8000
CMD ["./scripts/run_api.sh"]
