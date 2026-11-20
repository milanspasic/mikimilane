FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip xz-utils ca-certificates && rm -rf /var/lib/apt/lists/*
# Deno supplies yt-dlp's JavaScript runtime for public YouTube downloads.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/opt/deno sh
ENV PATH="/opt/deno/bin:${PATH}" SETLENS_HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py audiotag.py index.html ./
EXPOSE 8765
CMD ["python", "server.py"]
