# api/main.py
from fastapi import FastAPI
from routers import users, screenshots, logs
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Screen Monitor App API", version="1.0.0")


# CORS settings - allow all origins for simplicity; adjust as needed for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     
    allow_credentials=False, 
    allow_methods=["*"],     
    allow_headers=["*"],     
)


@app.get("/health",
         summary="Health check",
         description="Checks if the API is up and running."
)
def health():
    return {"ok": True}

# Include routers
app.include_router(users.router)
app.include_router(screenshots.router)
app.include_router(logs.router)


# [local] uvicorn app:app --reload --host 127.0.0.1 --port 8005 --env-file .env
# [remote] uvicorn app:app --host 0.0.0.0 --port 8000 --env-file .env