FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,sql,ml]"
COPY config ./config
COPY data/raw ./data/raw
EXPOSE 8010
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8010/api/health')" || exit 1
# Run the analysis once on start, then serve the dashboard + API
CMD ["sh", "-c", "python -m spareai run && python -m spareai serve --host 0.0.0.0 --port 8010"]
