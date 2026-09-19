FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bandwidtharr/ ./bandwidtharr/

RUN useradd --no-create-home --uid 1000 bandwidtharr \
    && mkdir -p /app/state \
    && chown bandwidtharr:bandwidtharr /app/state
USER bandwidtharr

# Placed after the expensive layers above (base image, pip install, code
# copy) so it changing on every commit doesn't bust their cache -- those
# layers only actually change when their own inputs do.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('WEB_PORT','80') + '/api/state', timeout=3)"

CMD ["python", "-m", "bandwidtharr.main"]
