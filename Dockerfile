FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py parsers.py ollama_client.py verifier.py ./

CMD ["python", "main.py"]
