FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir requests
COPY seerr_cleaner.py .

# Ecoute sur toutes les interfaces (necessaire en conteneur)
# et stocke config + backups dans le volume /data
ENV SEERR_CLEANER_HOST=0.0.0.0 \
    SEERR_CLEANER_DATA=/data

VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "seerr_cleaner.py"]
