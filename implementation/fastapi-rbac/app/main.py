from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import engine, Base
from app.rbac.cache import permission_cache
from app.rbac.exceptions import PermissionDenied, AuthenticationRequired
from app.api.v1 import router as api_v1_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    await permission_cache.connect()

    # Create tables (development only)
    if settings.DEBUG:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    yield

    # Shutdown
    await permission_cache.disconnect()


app = FastAPI(
    title=settings.APP_NAME,
    description="FastAPI Role-Based Access Control (RBAC) System",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Exception handlers
@app.exception_handler(PermissionDenied)
async def permission_denied_handler(request: Request, exc: PermissionDenied):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error": "permission_denied"}
    )


@app.exception_handler(AuthenticationRequired)
async def auth_required_handler(request: Request, exc: AuthenticationRequired):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error": "authentication_required"},
        headers=exc.headers
    )


# Include routers
app.include_router(api_v1_router)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": settings.APP_NAME}


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs"
    }
