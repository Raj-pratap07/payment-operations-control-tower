from fastapi import FastAPI

from app.core.config import settings
from app.routers.webhooks import router as webhooks_router


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION)
app.include_router(webhooks_router)
