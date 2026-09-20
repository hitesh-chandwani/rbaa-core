from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rbaa.config import Settings, get_settings
from rbaa.health import Probe, postgres_probe, redis_probe, unavailable_dependencies


def create_app(settings: Settings | None = None, probes: dict[str, Probe] | None = None) -> FastAPI:
    """Build the app. `probes` maps dependency name to an async check (tests inject fakes)."""
    settings = settings or get_settings()
    application = FastAPI(title="rbaa-core")
    application.state.probes = probes or {
        "postgres": postgres_probe(settings.database_url),
        "redis": redis_probe(settings.redis_url),
    }

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        down = await unavailable_dependencies(request.app.state.probes)
        if down:
            return JSONResponse({"status": "unavailable", "unavailable": down}, status_code=503)
        return JSONResponse({"status": "ready"})

    return application


app = create_app()
