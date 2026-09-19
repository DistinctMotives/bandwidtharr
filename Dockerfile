FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bandwidtharr/ ./bandwidtharr/

RUN useradd --no-create-home --uid 1000 bandwidtharr
USER bandwidtharr

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('WEB_PORT','80') + '/api/state', timeout=3)"

CMD ["python", "-m", "bandwidtharr.main"]
