FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY AgentRL/worker /upstream/worker
COPY AgentRL/proto /upstream/proto
RUN pip install --no-cache-dir /upstream/worker "mysql-connector-python~=9.3"
COPY AgentBench/src/server/tasks/dbbench /app/src/server/tasks/dbbench
COPY AgentBench/src/server/tasks/os_interaction /app/src/server/tasks/os_interaction
COPY AgentBench/data/dbbench /app/data/dbbench
COPY AgentBench/data/os_interaction /app/data/os_interaction
COPY configs /app/configs
COPY configure_image.py export_catalog.py /app/
RUN python configure_image.py
ENTRYPOINT ["python", "-m", "agentrl.worker"]
