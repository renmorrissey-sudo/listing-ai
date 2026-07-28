web: python -m migrations.runner && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
worker: python -m workers.sms_campaign_worker
