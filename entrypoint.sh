#!/bin/sh
# Force port 8000 to match Railway settings (ignoring dynamic PORT variable)
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
