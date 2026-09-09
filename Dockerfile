FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY config/ ./config/

# Install bridge package
RUN pip install --no-cache-dir .

# Expose bridge port (loopback bound by default)
EXPOSE 8765

# Set entrypoint
ENTRYPOINT ["hermes-antigravity-bridge"]
CMD ["--config", "config/config.example.toml", "serve"]
