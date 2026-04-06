FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tests run at build time — image won't be created if they fail
RUN python -m pytest tests/ -q

ENV PORT=5000

EXPOSE 5000

CMD ["python", "app.py"]
