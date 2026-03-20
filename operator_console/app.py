from pathlib import Path
import json

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from operator_console.bridge_api import BridgeAPI, BridgeAPIError, BridgeNotFoundError
from operator_console.config import ConsoleSettings, console_settings
from operator_console.data import RepoData


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
PROPOSAL_STATUSES = ["pending", "approved", "rejected", "executing", "executed", "failed"]


def create_app(
    *,
    settings: ConsoleSettings | None = None,
    bridge_api: BridgeAPI | None = None,
    repo_data: RepoData | None = None,
) -> FastAPI:
    settings = settings or console_settings()
    bridge_api = bridge_api or BridgeAPI(
        base_url=settings.bridge_url,
        operator_token=settings.operator_token,
    )
    repo_data = repo_data or RepoData(settings)

    app = FastAPI(title="RSI Operator Console")
    app.state.settings = settings
    app.state.bridge_api = bridge_api
    app.state.repo_data = repo_data
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["pretty_json"] = lambda value: json.dumps(value, indent=2, sort_keys=True)
    templates.env.filters["tone"] = status_tone
    app.state.templates = templates

    def render_page(
        request: Request,
        template_name: str,
        *,
        status_code: int = 200,
        **context,
    ) -> HTMLResponse:
        base_context = {
            "request": request,
            "bridge_url": settings.bridge_url,
            "workspace_dir": str(settings.workspace_dir),
            "trusted_state_dir": str(settings.trusted_state_dir),
            "proposal_statuses": PROPOSAL_STATUSES,
        }
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={**base_context, **context},
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        bridge_error = ""
        status = None
        latest_pending = None
        try:
            status = await bridge_api.get_status()
            pending = await bridge_api.list_proposals(status="pending")
            latest_pending = pending[0] if pending else None
        except BridgeAPIError as exc:
            bridge_error = str(exc)

        runs = repo_data.list_run_summaries()
        latest_run = runs[0] if runs else None
        return render_page(
            request,
            "home.html",
            page_title="Operator Console",
            status=status,
            bridge_error=bridge_error,
            latest_run=latest_run,
            latest_pending=latest_pending,
            run_count=len(runs),
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs(request: Request) -> HTMLResponse:
        return render_page(
            request,
            "runs.html",
            page_title="Runs",
            runs=repo_data.list_run_summaries(),
        )

    @app.get("/runs/{run_name}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_name: str) -> HTMLResponse:
        try:
            detail = repo_data.load_run_detail(run_name)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="run not found")
        return render_page(
            request,
            "run_detail.html",
            page_title=detail.summary.name,
            detail=detail,
        )

    @app.get("/proposals", response_class=HTMLResponse)
    async def proposals(request: Request, status: str | None = None) -> HTMLResponse:
        selected_status = status if status in PROPOSAL_STATUSES else None
        bridge_error = ""
        proposals = []
        try:
            proposals = await bridge_api.list_proposals(status=selected_status)
        except BridgeAPIError as exc:
            bridge_error = str(exc)
        return render_page(
            request,
            "proposals.html",
            page_title="Proposals",
            proposals=proposals,
            selected_status=selected_status,
            bridge_error=bridge_error,
        )

    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    async def proposal_detail(request: Request, proposal_id: str) -> HTMLResponse:
        bridge_error = ""
        proposal = None
        status_code = 200
        try:
            proposal = await bridge_api.get_proposal(proposal_id)
        except BridgeNotFoundError:
            status_code = 404
        except BridgeAPIError as exc:
            bridge_error = str(exc)
        return render_page(
            request,
            "proposal_detail.html",
            page_title=f"Proposal {proposal_id}",
            proposal_id=proposal_id,
            proposal=proposal,
            bridge_error=bridge_error,
            status_code=status_code,
        )

    @app.get("/artifacts/{artifact_path:path}")
    async def artifact_view(request: Request, artifact_path: str):
        try:
            artifact = repo_data.load_artifact(artifact_path)
        except ValueError:
            raise HTTPException(status_code=404, detail="artifact path rejected")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact not found")

        if artifact.kind == "image":
            assert artifact.path is not None
            return FileResponse(artifact.path)

        return render_page(
            request,
            "artifact.html",
            page_title=artifact.name,
            artifact=artifact,
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


def status_tone(reachable: bool) -> str:
    return "ok" if reachable else "bad"


app = create_app()
