FROM python:3.12-slim

# Install Node.js for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . 2>/dev/null || true

# Copy source
COPY . .
RUN pip install --no-cache-dir -e .

# State volume
VOLUME /app/logs

# Default: autonomous mode, 2-minute intervals
ENTRYPOINT ["near-agent"]
CMD ["run", "-i", "120"]
