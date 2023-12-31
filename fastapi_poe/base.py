import argparse
import copy
import json
import logging
import os
import sys
import warnings
from typing import Any, AsyncIterable, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi_poe.types import (
    ContentType,
    ErrorResponse,
    MetaResponse,
    PartialResponse,
    QueryRequest,
    ReportErrorRequest,
    ReportFeedbackRequest,
    SettingsRequest,
    SettingsResponse,
)

logger = logging.getLogger("uvicorn.default")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def set_body(self, request: Request):
        receive_ = await request._receive()

        async def receive():
            return receive_

        request._receive = receive

    async def dispatch(self, request: Request, call_next):
        logger.info(f"Request: {request.method} {request.url}")
        try:
            # Per https://github.com/tiangolo/fastapi/issues/394#issuecomment-927272627
            # to avoid blocking.
            await self.set_body(request)
            body = await request.json()
            logger.debug(f"Request body: {json.dumps(body)}")
        except json.JSONDecodeError:
            logger.error("Request body: Unable to parse JSON")

        response = await call_next(request)

        logger.info(f"Response status: {response.status_code}")
        try:
            if hasattr(response, "body"):
                body = json.loads(response.body.decode())
                logger.debug(f"Response body: {json.dumps(body)}")
        except json.JSONDecodeError:
            logger.error("Response body: Unable to parse JSON")

        return response


def exception_handler(request: Request, ex: HTTPException):
    logger.error(ex)


http_bearer = HTTPBearer()


def auth_user(
    authorization: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> None:
    if auth_key is None:
        return
    if authorization.scheme != "Bearer" or authorization.credentials != auth_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid access key",
            headers={"WWW-Authenticate": "Bearer"},
        )


class PoeBot:
    # Override these for your bot

    async def get_response(
        self, request: QueryRequest
    ) -> AsyncIterable[Union[PartialResponse, ServerSentEvent]]:
        """Override this to return a response to user queries."""
        yield self.text_event("hello")

    async def get_settings(self, setting: SettingsRequest) -> SettingsResponse:
        """Override this to return non-standard settings."""
        return SettingsResponse()

    async def on_feedback(self, feedback_request: ReportFeedbackRequest) -> None:
        """Override this to record feedback from the user."""
        pass

    async def on_error(self, error_request: ReportErrorRequest) -> None:
        """Override this to record errors from the Poe server."""
        logger.error(f"Error from Poe server: {error_request}")

    # Helpers for generating responses
    @staticmethod
    def text_event(text: str) -> ServerSentEvent:
        return ServerSentEvent(data=json.dumps({"text": text}), event="text")

    @staticmethod
    def replace_response_event(text: str) -> ServerSentEvent:
        return ServerSentEvent(
            data=json.dumps({"text": text}), event="replace_response"
        )

    @staticmethod
    def done_event() -> ServerSentEvent:
        return ServerSentEvent(data="{}", event="done")

    @staticmethod
    def suggested_reply_event(text: str) -> ServerSentEvent:
        return ServerSentEvent(data=json.dumps({"text": text}), event="suggested_reply")

    @staticmethod
    def meta_event(
        *,
        content_type: ContentType = "text/markdown",
        refetch_settings: bool = False,
        linkify: bool = True,
        suggested_replies: bool = True,
    ) -> ServerSentEvent:
        return ServerSentEvent(
            data=json.dumps(
                {
                    "content_type": content_type,
                    "refetch_settings": refetch_settings,
                    "linkify": linkify,
                    "suggested_replies": suggested_replies,
                }
            ),
            event="meta",
        )

    @staticmethod
    def error_event(
        text: Optional[str] = None,
        *,
        allow_retry: bool = True,
        error_type: Optional[str] = None,
    ) -> ServerSentEvent:
        data: dict[str, Union[bool, str]] = {"allow_retry": allow_retry}
        if text is not None:
            data["text"] = text
        if error_type is not None:
            data["error_type"] = error_type
        return ServerSentEvent(data=json.dumps(data), event="error")

    # Internal handlers

    async def handle_report_feedback(
        self, feedback_request: ReportFeedbackRequest
    ) -> JSONResponse:
        await self.on_feedback(feedback_request)
        return JSONResponse({})

    async def handle_report_error(
        self, error_request: ReportErrorRequest
    ) -> JSONResponse:
        await self.on_error(error_request)
        return JSONResponse({})

    async def handle_settings(self, settings_request: SettingsRequest) -> JSONResponse:
        settings = await self.get_settings(settings_request)
        return JSONResponse(settings.dict())

    async def handle_query(
        self, request: QueryRequest
    ) -> AsyncIterable[ServerSentEvent]:
        try:
            async for event in self.get_response(request):
                if isinstance(event, ServerSentEvent):
                    yield event
                elif isinstance(event, ErrorResponse):
                    yield self.error_event(
                        event.text,
                        allow_retry=event.allow_retry,
                        error_type=event.error_type,
                    )
                elif isinstance(event, MetaResponse):
                    yield self.meta_event(
                        content_type=event.content_type,
                        refetch_settings=event.refetch_settings,
                        linkify=event.linkify,
                        suggested_replies=event.suggested_replies,
                    )
                elif event.is_suggested_reply:
                    yield self.suggested_reply_event(event.text)
                elif event.is_replace_response:
                    yield self.replace_response_event(event.text)
                else:
                    yield self.text_event(event.text)
        except Exception as e:
            logger.exception("Error responding to query")
            yield self.error_event(repr(e), allow_retry=False)
        yield self.done_event()


def _find_access_key(*, access_key: str, api_key: str) -> Optional[str]:
    """Figures out the access key.

    The order of preference is:
    1) access_key=
    2) $POE_ACCESS_KEY
    3) api_key=
    4) $POE_API_KEY

    """
    if access_key:
        return access_key

    environ_poe_access_key = os.environ.get("POE_ACCESS_KEY")
    if environ_poe_access_key:
        return environ_poe_access_key

    if api_key:
        warnings.warn(
            "usage of api_key is deprecated, pass your key using access_key instead",
            DeprecationWarning,
            stacklevel=3,
        )
        return api_key

    environ_poe_api_key = os.environ.get("POE_API_KEY")
    if environ_poe_api_key:
        warnings.warn(
            "usage of POE_API_KEY is deprecated, pass your key using POE_ACCESS_KEY instead",
            DeprecationWarning,
            stacklevel=3,
        )
        return environ_poe_api_key

    return None


def _verify_access_key(
    *, access_key: str, api_key: str, allow_without_key: bool = False
) -> Optional[str]:
    """Checks whether we have a valid access key and returns it."""
    _access_key = _find_access_key(access_key=access_key, api_key=api_key)
    if not _access_key:
        if allow_without_key:
            return None
        print(
            "Please provide an access key.\n"
            "You can get a key from the create_bot page at: https://poe.com/create_bot?server=1\n"
            "You can then pass the key using the access_key param to the run() or make_endpoint() "
            "functions, or by using the POE_ACCESS_KEY environment variable."
        )
        sys.exit(1)
    if len(_access_key) != 32:
        print("Invalid access key (should be 32 characters)")
        sys.exit(1)
    return _access_key


def make_endpoints(
    access_key: str = "",
    *,
    api_key: str = "",
    allow_without_key: bool = False,
):
    """Create an app object. Arguments are as for run()."""
    global auth_key
    auth_key = _verify_access_key(
        access_key=access_key, api_key=api_key, allow_without_key=allow_without_key
    )

    async def poe_post(request: dict[str, Any], bot: PoeBot) -> Response:
        if request["type"] == "query":
            return EventSourceResponse(
                bot.handle_query(
                    QueryRequest.model_validate(
                        {
                            **request,
                            "access_key": auth_key or "<missing>",
                            "api_key": auth_key or "<missing>",
                        }
                    )
                )
            )
        elif request["type"] == "settings":
            return await bot.handle_settings(SettingsRequest.model_validate(request))
        elif request["type"] == "report_feedback":
            return await bot.handle_report_feedback(
                ReportFeedbackRequest.model_validate(request)
            )
        elif request["type"] == "report_error":
            return await bot.handle_report_error(ReportErrorRequest.model_validate(request))
        else:
            raise HTTPException(status_code=501, detail="Unsupported request type")
    return poe_post
    


def run(
    app: FastAPI
) -> None:
    """
    Run a Poe bot server using FastAPI.

    :param bot: The bot object.
    :param access_key: The access key to use. If not provided, the server tries to read
    the POE_ACCESS_KEY environment variable. If that is not set, the server will
    refuse to start, unless *allow_without_key* is True.
    :param api_key: The previous name of access_key. This param is deprecated and will be
    removed in a future version
    :param allow_without_key: If True, the server will start even if no access key
    is provided. Requests will not be checked against any key. If an access key
    is provided, it is still checked.

    """
    parser = argparse.ArgumentParser("FastAPI sample Poe bot server")
    parser.add_argument("-p", "--port", type=int, default=8080)
    args = parser.parse_args()
    port = args.port

    logger.info("Starting")
    import uvicorn.config

    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["formatters"]["default"][
        "fmt"
    ] = "%(asctime)s - %(levelname)s - %(message)s"
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=log_config)
