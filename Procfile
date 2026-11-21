web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
worker: celery -A app.workers.message_worker.celery_app worker --loglevel=info --concurrency=2
