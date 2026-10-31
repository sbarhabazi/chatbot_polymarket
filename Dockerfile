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

# Fichiers d'état dans le volume monté (-v /opt/polybot/data:/app/data)
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

# Sécuriser les permissions
RUN chown -R botuser:botuser /app
USER botuser

# Healthcheck : le bot réécrit /app/data/heartbeat toutes les 30 s.
# Malsain si le fichier manque ou a plus de 180 s (bot arrêté ou boucle bloquée).
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/app/data/heartbeat') < 180 else 1)" || exit 1

# Point d'entrée
CMD ["python", "main.py"]
