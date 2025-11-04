Town of Shadows - FastAPI Enhanced Server

This package contains an enhanced FastAPI WebSocket server for Town of Shadows, configured to allow connections from your frontend at:
https://69095967f1d5a6e43a33739b--bright-sunshine-4f6996.netlify.app

Quick start (on Render):
1. pip install -r requirements.txt
2. uvicorn main:app --host 0.0.0.0 --port $PORT

Rooms are public by default. The WS endpoint is: wss://<your-server-host>/ws
