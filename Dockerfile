FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    curl \
    wget \
    libcurl4-openssl-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone the complete repository with modified gpm
RUN git clone https://github.com/andremxmx/mvsgoo.git .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create cache directory
RUN mkdir -p /app/video_cache

# Ensure gpm directory has proper permissions and structure
RUN chmod -R 755 /app/gpm
RUN find /app/gpm -name "*.py" -exec chmod 644 {} \;

# Set environment variables
ENV PYTHONPATH=/app:/app/gpm:/app/gpm/gpmc
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 7860

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:7860/ || exit 1

# Run the application
CMD ["python", "google_photos_api.py"]
