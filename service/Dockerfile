FROM python:3.12-slim

WORKDIR /app

# ไม่มี dependency ภายนอก — stdlib ล้วน
COPY main.py report.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    REPORT_TIMEZONE=Asia/Bangkok \
    DAILY_REPORT_AT=08:30 \
    DAILY_REPORT=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

CMD ["python", "main.py"]
