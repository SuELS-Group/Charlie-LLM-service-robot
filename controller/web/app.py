from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from web import state
from web.lifecycle import lifespan
from web.routes import admin, battery, faces, greeting, knowledge, machines, monitoring, voice_activity, voices

app = FastAPI(title="ROS2 Robot Controller", description="Web interface for controlling ROS2 robot nodes via systemd", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, )
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], )
app.include_router(machines.router)
app.include_router(battery.router)
app.include_router(admin.router)
app.include_router(faces.router)
app.include_router(voice_activity.router)
app.include_router(voices.router)
app.include_router(greeting.router)
app.include_router(knowledge.router)
app.include_router(monitoring.router)
if state.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(state.STATIC_DIR)), name="static")
