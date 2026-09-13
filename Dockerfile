FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY speedarr/ ./speedarr/

EXPOSE 80

CMD ["python", "-m", "speedarr.main"]
