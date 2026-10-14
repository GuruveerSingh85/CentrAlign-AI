import hmac
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


def install_api_middleware(app: FastAPI) -> None:
    frontend_origins = (
        os.getenv("FRONTEND_ORIGINS")
        or os.getenv("FRONTEND_ORIGIN")
        or "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in frontend_origins.split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_unified_workflow(request: Request, call_next):
        path = request.url.path
        expected_token = os.getenv("API_BEARER_TOKEN")
        if (
            expected_token
            and path.startswith("/api/")
            and path != "/api/health"
            and request.method != "OPTIONS"
        ):
            scheme, _, supplied_token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(supplied_token, expected_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "A valid bearer token is required."},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        trusted_user_header = os.getenv("TRUSTED_USER_HEADER", "").strip()
        identity_required = (
            trusted_user_header
            and path.startswith("/api/")
            and path != "/api/health"
            and request.method != "OPTIONS"
        )
        if identity_required:
            owner_id = request.headers.get(trusted_user_header, "").strip()
            if not owner_id or len(owner_id) > 128 or not all(
                character.isalnum() or character in "._@-" for character in owner_id
            ):
                return JSONResponse(status_code=401, content={"detail": "A trusted user identity is required."})
            request.state.owner_id = owner_id
        else:
            request.state.owner_id = "default"

        trusted_organization_header = os.getenv("TRUSTED_ORG_HEADER", "").strip()
        organization_id = (
            request.headers.get(trusted_organization_header, "").strip()
            if trusted_organization_header else ""
        )
        if organization_id and (
            len(organization_id) > 128
            or not all(character.isalnum() or character in "._@-" for character in organization_id)
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "A valid trusted organization identity is required."},
            )
        request.state.organization_id = organization_id or None

        manual_workflow_action = path.startswith("/api/legacy/") or (
            path.startswith("/api/tasks/")
            and path.rsplit("/", 1)[-1] in {
                "execute-policy",
                "execute",
                "vendor-proposal",
                "verify",
            }
        )
        if request.method == "POST" and manual_workflow_action:
            return JSONResponse(
                status_code=410,
                content={"detail": "This action is managed by POST /api/tasks/{task_id}/run."},
            )
        return await call_next(request)