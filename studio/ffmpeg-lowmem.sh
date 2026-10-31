#!/bin/sh
# Low-memory wrapper: limit decoder threads (ffmpeg auto-threads = all host cores, each with frame buffers).
exec "$FFMPEG_REAL" -threads "${MPT_FFMPEG_THREADS:-1}" "$@"
