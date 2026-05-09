"""Heroku / gunicorn entrypoint and `flask --app wsgi` target."""
from productsync import create_app

app = create_app()
