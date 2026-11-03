FROM python:3.12-slim
RUN apt-get update \
	&& apt-get install -y --no-install-recommends libimage-exiftool-perl postgresql-client ffmpeg \
	&& rm -rf /var/lib/apt/lists/* \
	&& pip install --no-cache-dir pillow pillow-heif
WORKDIR /app
COPY app.py index.html ./
EXPOSE 8791
CMD ["python3", "-u", "app.py"]
