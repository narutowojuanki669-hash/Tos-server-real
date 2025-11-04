from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# ✅ allow your specific Netlify frontend
origins = [
    "https://690a4beb4e174c424fe59d8c--townofshadows.netlify.app",
    "http://localhost:3000",  # optional for testing
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/test")
def read_root():
    return "Hello from Town of Shadows backend!"
