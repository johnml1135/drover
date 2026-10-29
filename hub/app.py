"""App factory: wires store, bus, auth, routers, SSE and the watchdog."""
from drover import placement
from drover.placement_git import request_git
import time
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from hub import api, auth, events, notifications, outside, web, tree_web
from hub.perf import Perf
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
    previous_placement = None
    dog = Watchdog(store, settings, bus=bus, wsl=wsl)
    dispatch = DispatcherService(store, settings, make_dispatcher=make_dispatcher)
    services = services or Services(store, settings, wsl=wsl)
    app = FastAPI(title="Drover Hub")
    app.state.perf = Perf()
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
        nonlocal previous_placement
        previous_placement = placement.bind_settings(store.get_settings)
        if settings.run_watchdog:
            dog.start()
        if settings.run_dispatcher:
            dispatch.start()
        if settings.run_services:
            services.start()
        ui_view.refresh_repos()  # first page load must not wait for the git config reads
    def shutdown():
        services.stop()
        dispatch.stop()
        dog.stop()
        ui_view.close()
        if previous_placement is not None:
            placement.bind_settings(previous_placement)
        store.close()
    app.state.start_background = start_background
    app.state.shutdown = shutdown
    ui_view = UiView(store, services=services, watchdog=dog, dispatcher=dispatch,
                     runs_dir=settings.runs_dir)
    def invalidate_on_change(kind, data):
        event_kind = data.get("kind", "") if kind in ("ledger", "event") else kind
        if event_kind.startswith(("repo.", "config", "landing", "origin")) or event_kind == "settings":
            ui_view.invalidate_repos()
    bus.on_publish = invalidate_on_change
    app.state.ui_view = ui_view
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        started = time.perf_counter()
        counts = {"spawns": 0}
        token = request_git.set(counts)
        try:
            on_api_port = auth.arrived_on(request) == settings.api_port != settings.ui_port
            if path.startswith("/api/v1"):
                response = await call_next(request)
            elif "authorization" in request.headers or on_api_port or not auth.ui_user(request, settings):
                response = JSONResponse({"detail": "forbidden"}, status_code=403)
            elif not auth.csrf_ok(request):
                response = JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            else:
                response = await call_next(request)
        except Exception:
            logging.getLogger("hub").exception("unhandled request error")
            response = PlainTextResponse("Internal Server Error", status_code=500)
        finally:
            duration = (time.perf_counter() - started) * 1000
            request_git.reset(token)
        route = getattr(request.scope.get("route"), "path", path)
        render = getattr(request.state, "render_ms", None)
        response.headers["Server-Timing"] = f'total;dur={duration:.2f}, git;desc="{counts["spawns"]} spawns"' + (f", render;dur={render:.2f}" if render is not None else "")
        body = getattr(response, "body_iterator", None)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return response  # a long-lived stream is not a slow request
        if body is None:
            app.state.perf.record(route, request.method, duration, counts["spawns"])
            return response
        async def timed_body():  # perf covers the time to send the body, not only to build it
            try:
                async for chunk in body:
                    yield chunk
            finally:
                app.state.perf.record(route, request.method, (time.perf_counter() - started) * 1000, counts["spawns"])
        response.body_iterator = timed_body()
        return response
    for exc, code in ((NotFound, 404), (BadRequest, 400), (Forbidden, 403)):  # the website's store errors
        app.add_exception_handler(exc, lambda r, e, code=code: JSONResponse({"detail": str(e)}, status_code=code))
    app.include_router(api.public)
    app.include_router(api.router)
    app.include_router(notifications.router)
    app.include_router(outside.router)
    app.include_router(events.router)
    app.dependency_overrides[web.get_store] = lambda: ui_view
    app.include_router(web.router)
    app.include_router(tree_web.router)
    return app
