FROM python:3.11-slim
WORKDIR /app
COPY requirements-studio.txt .
RUN pip install --no-cache-dir -r requirements-studio.txt
RUN ln -s "$(python -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())')" /usr/local/bin/ffmpeg-real && /usr/local/bin/ffmpeg-real -version | head -1
COPY . .
RUN cp studio/ffmpeg-lowmem.sh /usr/local/bin/ffmpeg && chmod +x /usr/local/bin/ffmpeg
ENV PYTHONUNBUFFERED=1 PORT=10000 MPT_MAX_720P=1 MALLOC_ARENA_MAX=2 OMP_NUM_THREADS=1 MPT_X264_PRESET=veryfast FFMPEG_REAL=/usr/local/bin/ffmpeg-real IMAGEIO_FFMPEG_EXE=/usr/local/bin/ffmpeg MPT_FFMPEG_FINAL=1
EXPOSE 10000
CMD ["python", "studio/server.py"]
