from django.db import models


class ChatSession(models.Model):
    """Conversation memory. Keyed by (session_id, dealer) so two dealers can never read each other's chat."""

    session_id = models.CharField(max_length=128)
    # CASCADE: a chat transcript has no value without its dealer and holds no financial data.
    dealer = models.ForeignKey("catalog.Dealer", on_delete=models.CASCADE, related_name="chat_sessions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session_id", "dealer"], name="one_session_per_dealer")]


class ChatMessage(models.Model):
    USER, ASSISTANT = "user", "assistant"

    # CASCADE: messages belong to exactly one session.
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=[(USER, USER), (ASSISTANT, ASSISTANT)])
    content = models.TextField()
    tool_calls = models.JSONField(default=list, blank=True)  # audit: what the model called this turn
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
