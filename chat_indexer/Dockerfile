FROM python:3.12-slim
WORKDIR /app
COPY chat_indexer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY chat_indexer/indexer.py ./indexer.py
CMD ["python", "indexer.py"]

