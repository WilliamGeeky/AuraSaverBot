FROM python:3.12-slim

# ffmpeg — склейка 720p/1080p; nodejs — JS-рантайм для yt-dlp (без него
# YouTube на серверных IP (Render и др.) часто отдаёт только SABR/storyboard).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py downloader.py ./

CMD ["python", "bot.py"]
