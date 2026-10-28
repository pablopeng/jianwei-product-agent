FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 AGENT_DEPLOYMENT=cloud AGENT_STATE_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home agent && mkdir /data && chown agent:agent /data /app
COPY --chown=agent:agent . .
USER agent
EXPOSE 8080
CMD ["python", "run.py"]
