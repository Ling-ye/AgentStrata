"""The Windows companion's Owner tools over its existing local MCP transport."""
from __future__ import annotations

from contextlib import asynccontextmanager

from . import voice_jobs


def register(app, runtime):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("AgentStrata character studio", streamable_http_path="/",
                 stateless_http=True, json_response=True)

    @mcp.tool()
    def character_generate(name: str, identity: str, response_style: str, artwork: str,
                           sources: list[dict[str, str]] | None = None, request_id: str | None = None) -> dict:
        """Create a role after the Owner asks. Draft identity, response style and the image prompt.
        Use the configured search/fetch tools to find original public voice pages. Pass candidates with
        artist, title, kind (speech/song), language (zh/ja/en/ko/yue), page, audio and license where
        verified. Registered local recordings use upload_id. No supported source leaves the image
        and prompts available while the job waits for audio. This operation never activates the role.
        """
        return voice_jobs.generate(runtime, name=name, identity=identity, response_style=response_style,
                                   artwork=artwork, sources=sources, request_id=request_id)

    @mcp.tool()
    def character_train_voice(character: str, version: int, sources: list[dict[str, str]] | None = None,
                              request_id: str | None = None) -> dict:
        """Train a new voice for an existing character/version, reusing its identity and model.
        The sources have the same fields as character_generate. Inspect status without resubmitting.
        """
        return voice_jobs.train_voice(runtime, character=character, version=version,
                                      sources=sources, request_id=request_id)

    @mcp.tool()
    def character_job_status(job: str) -> dict:
        """Read the original character job to find progress, missing inputs or the new library version."""
        row = runtime.get(job)
        return {key: row[key] for key in ("id", "status", "stage", "character_id", "character_version",
                                          "error", "error_code", "library_error") if key in row}

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application), mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.mount("/agentstrata/mcp", mcp.streamable_http_app())
