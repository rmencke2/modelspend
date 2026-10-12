FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MODELSPEND_HOME=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd -m -u 10001 modelspend && mkdir /data && chown modelspend /data
USER modelspend
VOLUME /data
EXPOSE 8787
# Binding 0.0.0.0 inside the container requires MODELSPEND_API_TOKEN (the server refuses otherwise).
# Publish the port on loopback only: docker run -p 127.0.0.1:8787:8787 ...
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"
CMD ["modelspend", "serve", "--host", "0.0.0.0", "--port", "8787"]
