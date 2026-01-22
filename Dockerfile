FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "bot.runner:app", "--host", "0.0.0.0", "--port", "8000"]