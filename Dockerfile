FROM python:3.12-slim
RUN apt-get update \
	&& apt-get install -y --no-install-recommends libimage-exiftool-perl postgresql-client ffmpeg \
	&& rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html ./
EXPOSE 8791
CMD ["python3", "-u", "app.py"]
