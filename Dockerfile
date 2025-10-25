FROM ubuntu:latest
LABEL authors="den"

ENTRYPOINT ["top", "-b"]

FROM python:3.13-slim

RUN apt-get update && apt-get install -y iputils-ping traceroute dnsutils && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py database.py models.py smtp.py keys.py ./
COPY frontend/ frontend/

ENV SERVICE=backend

# Запуск в зависимости от сервиса
CMD ["sh", "-c", "if [ \"$SERVICE\" = \"backend\" ]; then uvicorn main:app --host 0.0.0.0 --port 8000; else streamlit run frontend/app.py --server.port 8501 --server.address 0.0.0.0; fi"]