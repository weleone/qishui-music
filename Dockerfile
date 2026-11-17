FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# ffmpeg + 桌面依赖(无头模式最小集)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir flask requests "playwright==1.49.1" && \
    python -c "from playwright.sync_api import sync_playwright; print('playwright ok')" && \
    ffmpeg -version | head -1

WORKDIR /app
COPY app.py qishui.py ./
COPY static/ ./static/

ENV PORT=8080 \
    WORK_DIR=/tmp/qishui \
    CLEANUP_AFTER=3600

EXPOSE 8080
CMD ["python", "app.py"]
