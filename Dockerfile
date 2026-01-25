FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y \
    libssl-dev \
    libxss1 \
    libnss3 \
    libappindicator1 \
    libindicator7 \
    fonts-liberation \
    fonts-dejavu \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "bot.runner:app", "--host", "0.0.0.0", "--port", "8000"]