from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()  # ✅ define the app first

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://690a605759df9864bca76c4c--resonant-cobbler-2fa697.netlify.app",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/test")
def read_root():
    return {"message": "Hello from Town of Shadows backend!"}
