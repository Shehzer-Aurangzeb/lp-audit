FROM node:22-bookworm-slim
ARG TARGETARCH

# Python + Playwright (Chromium for the real-visitor test, GTmetrix and Lighthouse)
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates curl git php-cli \
 && pip3 install --no-cache-dir --break-system-packages playwright==1.49.1 semgrep==1.97.0 \
 && python3 -m playwright install --with-deps chromium \
 && npm install -g lighthouse@12.6.1 \
 && rm -rf /var/lib/apt/lists/* /root/.npm

# Code audit tools: gitleaks (secrets) and PHPStan (PHP errors)
RUN A=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64) \
 && curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_${A}.tar.gz" | tar -xz -C /usr/local/bin gitleaks \
 && curl -sSL -o /usr/local/bin/phpstan "https://github.com/phpstan/phpstan/releases/download/1.12.11/phpstan.phar" \
 && chmod +x /usr/local/bin/phpstan \
 && git config --system --add safe.directory '*'

WORKDIR /app
COPY audit.py rules.json code_rules.json manual_checklist.json ./
ENTRYPOINT ["python3", "/app/audit.py"]
