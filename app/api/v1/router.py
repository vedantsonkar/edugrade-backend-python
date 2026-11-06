"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1.routes import (
    admin,
    analytics,
    assessments,
    assignments,
    attempts,
    auth,
    cron,
    files,
    grading,
    health,
    profile,
    school,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(school.router)
api_router.include_router(admin.router)
api_router.include_router(assignments.router)
api_router.include_router(assessments.router)
api_router.include_router(attempts.router)
api_router.include_router(grading.router)
api_router.include_router(files.router)
api_router.include_router(profile.router)
api_router.include_router(analytics.router)
api_router.include_router(cron.router)
