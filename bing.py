from typing import Literal
from fastapi_poe import PoeBot
from fastapi_poe.types import PartialResponse, QueryRequest
from sydney import SydneyClient


T_conversation = dict[Literal["lastQuestion", "mode", "client"], str|SydneyClient]
conversations: list[T_conversation] = []
conversation: None|T_conversation = None


class BingChatBot(PoeBot):
  def __init__(self, mode: Literal['balanced', 'creative', 'precise']) -> None:
    super().__init__()
    self.mode = mode
  
  async def get_response(
        self, request: QueryRequest
  ):
    user_query = request.query[-1].content
    
    for i in range(len(conversations)-1, -1, -1):
      last_question = conversations[i]['lastQuestion']
      if len(request.query) < 2: break
      if conversations[i]['mode'] == self.mode and (last_question == request.query[-3].content or (last_question == request.query[-2].content and request.query[-2].role == 'user')):
        conversations[i]["lastQuestion"] = user_query
        conversation = conversations[i]
        break
    
    if not conversation:
      if len(request.query) > 2:
        yield PartialResponse(text="\n###Somthing goes wrong, the context was cleared\n\n")
      conversation = {
        "lastQuestion": user_query,
        "mode": self.mode,
        "client": SydneyClient(style=self.mode)
      }
      conversations.append(conversation)
      await conversation["client"].start_conversation()
    
    async for response, suggestion in conversation["client"].ask_stream(user_query, citations=True, suggestions=True):
      if suggestion:
        yield PartialResponse(text=suggestion, is_suggested_reply=True)
      if response:
        yield PartialResponse(text=response)