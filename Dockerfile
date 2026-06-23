FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/agent_workspace

ENV DATABASE_PATH=/app/data/chat.db
ENV AGENT_WORKSPACE=/app/agent_workspace

VOLUME ["/app/data", "/app/agent_workspace"]

EXPOSE 5000

CMD ["python", "app.py"]
