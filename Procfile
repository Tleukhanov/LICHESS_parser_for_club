release: python -c "from backend.db.database import init_db; init_db()"
web: python -m uvicorn app:app --host 0.0.0.0 --port $PORT
