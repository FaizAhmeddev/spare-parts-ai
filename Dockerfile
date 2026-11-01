FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SPAREAI_HOST=0.0.0.0
# libgomp1 = OpenMP runtime needed by LightGBM / scikit-learn on slim images
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,sql,ml]"
COPY config ./config
COPY data/raw ./data/raw
EXPOSE 8010
HEALTHCHECK CMD python -c "import os,urllib.request;urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT','8010'))" || exit 1
# Open the port immediately (cloud health checks), run the analysis in the background.
# Uses $PORT when the platform provides it (Render, Railway, Cloud Run), else 8010.
CMD ["python", "-m", "spareai", "serve", "--run-on-start"]