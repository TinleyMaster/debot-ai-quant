FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ .

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "main.py"]
