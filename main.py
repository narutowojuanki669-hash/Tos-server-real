from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS setup for your Netlify frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bright-sunshine-4f6996.netlify.app",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "Welcome to Town of Shadows backend!"}

@app.get("/test")
def test_route():
    return {"message": "Server is running correctly!"}

@app.get("/players")
def get_players():
    return {"players": []}
