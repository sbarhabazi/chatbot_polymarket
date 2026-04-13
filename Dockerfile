FROM python:3.11-slim

# Sécurité : ne pas tourner en root
RUN useradd -m -u 1000 botuser

WORKDIR /app

# Dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copier le code
COPY . .

# Sécuriser les permissions
RUN chown -R botuser:botuser /app
USER botuser

# Healthcheck toutes les 60s
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; open('/app/healthcheck.flag').read()" || exit 1

# Point d'entrée
CMD ["python", "main.py"]
