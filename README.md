Town of Shadows - Ready backend (Upload to Render)
=================================================

This package contains a ready-to-deploy FastAPI backend for Town of Shadows.
It includes REST endpoints and a WebSocket endpoint for real-time use.

How to deploy on Render:
1. Create a new Web Service -> deploy from ZIP (upload this ZIP)
2. Start command: uvicorn main:app --host 0.0.0.0 --port 10000
