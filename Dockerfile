FROM python:3.10-slim

  WORKDIR /app

  RUN apt-get update && apt-get install -y gcc g++ ffmpeg && rm -rf /var/lib/apt/lists/*

  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt

  COPY src/ src/
  COPY web/ web/

  RUN mkdir -p data/outputs/downloads

  EXPOSE 10000

  CMD uvicorn src.instagram_scraper.main:app --host 0.0.0.0 --port ${PORT:-10000}
