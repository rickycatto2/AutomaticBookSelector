FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IN_DOCKER=1 DATA_DIR=/data HOST=0.0.0.0 PORT=5077 GOODREADS_IMPORT_DIR=/imports
WORKDIR /app
COPY core.py server.py ./
COPY static ./static
RUN useradd --uid 10001 --create-home reader && mkdir /data /imports && chown reader:reader /data /imports
USER reader
EXPOSE 5077
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5077/api/health', timeout=3)"
CMD ["python", "server.py"]
