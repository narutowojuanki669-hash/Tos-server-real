from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()  # ✅ define the app first

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://690a4beb4e174c424fe59d8c--townofshadows.netlify.app",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/test")
def read_root():
    return {"message": "Hello from Town of Shadows backend!"}
