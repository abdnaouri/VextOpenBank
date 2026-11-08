FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=10000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["python3", "-m", "uvicorn", "vext_open_bank.main:app", "--host", "0.0.0.0", "--port", "10000"]
