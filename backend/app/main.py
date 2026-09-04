from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers.webhooks import router as webhooks_router
from app.routers.api import router as api_router


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION)
app.add_middleware(
	CORSMiddleware,
	allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
	allow_methods=["GET", "POST", "OPTIONS"],
	allow_headers=["Accept", "Content-Type"],
)
app.include_router(webhooks_router)
app.include_router(api_router)
