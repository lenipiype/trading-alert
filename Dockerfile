FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py core.py providers.py alarms.py history.py ./
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "app.py"]
CMD ["run"]
