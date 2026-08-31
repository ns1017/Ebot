"""
FastAPI dashboard. main.py sets app.state.db_path and app.state.config
at startup, then runs this alongside the scrape loop.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from modules import db

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

app = FastAPI(title="Automotive Listings Dashboard")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.get("/")
async def index(request: Request, target: str | None = None, verdict: str | None = None, outliers_only: bool = False):
    db_path = request.app.state.db_path
    listings = await db.fetch_listings(db_path, target=target, verdict=verdict, outliers_only=outliers_only)
    targets = await db.distinct_targets(db_path)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "listings": listings,
            "targets": targets,
            "current_target": target or "",
            "current_verdict": verdict or "",
            "outliers_only": outliers_only,
        },
    )


@app.get("/api/listings")
async def api_listings(target: str | None = None, verdict: str | None = None, outliers_only: bool = False):
    db_path = app.state.db_path
    listings = await db.fetch_listings(db_path, target=target, verdict=verdict, outliers_only=outliers_only)
    return JSONResponse(listings)
