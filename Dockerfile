FROM python:3.12-slim

# Install Node.js (for Claude Code CLI + npm test execution)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install GitHub CLI (for code delivery)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Copy source
COPY . .
RUN pip install --no-cache-dir -e .

# State volume
VOLUME /app/logs

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["near-agent"]
CMD ["run", "-i", "120"]
