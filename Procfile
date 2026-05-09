web: gunicorn wsgi:app
worker: rq worker -u $REDIS_URL productsync-default
release: flask db upgrade
