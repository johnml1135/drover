"""App factory: wires store, bus, auth, routers, SSE and the watchdog."""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse

from hub import api, auth, events, web
from hub.dispatch import DispatcherService
from hub.services import Services
from hub.settings import Settings
from hub.store import BadRequest, Forbidden, NotFound, Store, UiView
from hub.watchdog import Watchdog, real_wsl


def create_app(db_path, settings: Settings | None = None, wsl=real_wsl, make_dispatcher=None,
               services=None) -> FastAPI:
    settings = settings or Settings()
    bus = events.Bus()
    store = Store(db_path, publish=bus.publish)
    dog = Watchdog(store, settings, bus=bus, wsl=wsl)
    dispatch = DispatcherService(store, settings, make_dispatcher=make_dispatcher)
    services = services or Services(store, settings, wsl=wsl)

    # Lifecycle is owned by main(), not by uvicorn: several listeners share this
    # app and a listener rebind must never stop the watchdog or close the store.
    app = FastAPI(title="Drover Hub")
    app.state.settings = settings
    app.state.token = auth.load_token(settings)
    app.state.allowed_logins = auth.resolve_allowed_logins(settings, logging.getLogger('hub'))
    app.state.store = store
    app.state.bus = bus
    app.state.watchdog = dog
    app.state.dispatcher = dispatch
    app.state.services = services
    app.state.allowed_hosts = auth.resolve_allowed_hosts(settings)

    def start_background():
        if settings.run_watchdog:
            dog.start()
        if settings.run_dispatcher:
            dispatch.start()
        if settings.run_services:
            services.start()

    def shutdown():
        services.stop()
        dispatch.stop()
        dog.stop()
        ui_view.repo_states.close()
        store.close()

    app.state.start_background = start_background
    app.state.shutdown = shutdown
    ui_view = UiView(store, services=services, watchdog=dog, dispatcher=dispatch,
                     runs_dir=settings.runs_dir)

    app.state.ui_view = ui_view

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        on_api_port = auth.arrived_on(request) == settings.api_port != settings.ui_port
        if path.startswith("/api/v1"):
            return await call_next(request)  # route dependencies authorise
        if on_api_port or not auth.ui_user(request, settings):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if not auth.csrf_ok(request):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    for exc, code in ((NotFound, 404), (BadRequest, 400), (Forbidden, 403)):  # the website's store errors
        app.add_exception_handler(exc, lambda r, e, code=code: JSONResponse({"detail": str(e)}, status_code=code))

    app.include_router(api.public)
    app.include_router(api.router)

    @app.get("/events", response_class=EventSourceResponse)
    async def sse(request: Request, limit: int | None = None):
        last = request.headers.get("last-event-id")
        after = int(last) if last and last.isdigit() else None
        async for ev in events.stream(bus, after, limit):
            yield ev

    app.dependency_overrides[web.get_store] = lambda: ui_view
    app.include_router(web.router)
    return app
