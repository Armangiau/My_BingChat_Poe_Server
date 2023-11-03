from typing import Any
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from bing import BingChatBot
from fastapi_poe import exception_handler, make_endpoints

load_dotenv()

app = FastAPI()
poe_post = make_endpoints("")
app.add_exception_handler(RequestValidationError, exception_handler)

@app.get("/")
async def index() -> Response:
    url = "https://poe.com/create_bot?server=1"
    return HTMLResponse(
        "<html><body><h1>FastAPI Poe bot server</h1><p>Congratulations! Your server"
        " is running. To connect it to Poe, create a bot at <a"
        f' href="{url}">{url}</a>.</p></body></html>'
    )

@app.post("bing/{mode}")
def bing(mode: str, request: dict[str, Any]):
    return poe_post(request, BingChatBot(mode))