"""Select local Ollama or authenticated Modal inference with shared prompts."""

from app.ollama_client import OllamaClient, AssistantUnavailableError


class ModalQwenClient(OllamaClient):
    def __init__(self, app_name='landlord-qwen'):
        self.app_name = app_name

    def health_check(self):
        import modal
        return [modal.Function.from_name(self.app_name, 'model_status').remote()['model']]

    def _chat(self, messages, *, json_mode):
        import modal
        try:
            model = modal.Cls.from_name(self.app_name, 'Qwen')()
            return model.chat.remote(messages, json_mode)
        except Exception:
            raise AssistantUnavailableError('Modal Qwen inference is unavailable') from None


def create_assistant(settings):
    if settings.ai_backend == 'modal':
        return ModalQwenClient(settings.modal_qwen_app)
    return OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
