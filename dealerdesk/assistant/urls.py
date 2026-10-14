from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError
from core.validation import clean_text, raise_if, require_object
from orders.services import validate_dealer

from .agent import run_turn
from .llm import LLMUnavailable


class ChatView(APIView):
    def post(self, request):
        data = dict(require_object(request.data))
        if isinstance(data.get("session_id"), int) and not isinstance(data.get("session_id"), bool):
            data["session_id"] = str(data["session_id"])
        errors = []
        session_id = clean_text(data, "session_id", required=True, max_length=128, errors=errors)
        message = clean_text(data, "message", required=True, max_length=2000, errors=errors)
        raise_if(errors)
        dealer = validate_dealer(data.get("dealer_id"))
        try:
            return Response(run_turn(dealer, session_id, message))
        except LLMUnavailable as exc:
            raise ApiError(503, "ASSISTANT_UNAVAILABLE", f"The assistant's language model is unavailable: {exc}")


urlpatterns = [path("assistant/chat/", ChatView.as_view())]
