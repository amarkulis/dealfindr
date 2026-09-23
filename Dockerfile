# DealFindr cron image — daily cold-brew coffee deal scanner.
# Python 3.11 base + Playwright/Chromium for sites that need a headless browser.
FROM python:3.11-slim

# Avoid Python writing .pyc files and buffering stdout/stderr.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for Playwright Chromium + general runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binary + OS libs that Playwright needs.
RUN playwright install chromium --with-deps

# Copy the application source.
COPY dealfindr.py dealfindr_cron.py router_dealfindr.py ./

# Run the cron entrypoint.
CMD ["python3", "-u", "dealfindr_cron.py"]