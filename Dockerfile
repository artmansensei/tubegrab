# syntax=docker/dockerfile:1

FROM python:3.11-slim

# System deps: ffmpeg + node (yt-dlp needs a JS runtime for YouTube signatures)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face runs containers as a non-root user (uid 1000)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app

# Install Python deps first (better layer caching)
COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy the app
COPY --chown=user . .

# Hugging Face Spaces expose port 7860
ENV PORT=7860 \
    HOST=0.0.0.0 \
    DOWNLOAD_DIR=/data/downloads

# Persistent storage mount point (HF Spaces provides /data)
RUN mkdir -p /data/downloads 2>/dev/null || true

EXPOSE 7860

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-7860}"]