"""Private Qwen inference. Prepare weights on CPU before enabling cloud AI."""

import subprocess
import time

import modal

APP_NAME = 'landlord-qwen'
MODEL = 'qwen3.5:4b-q4_K_M'
MODEL_DIR = '/models'
app = modal.App(APP_NAME)
weights = modal.Volume.from_name('landlord-qwen-models', create_if_missing=True)
image = (modal.Image.from_registry('ollama/ollama:latest', add_python='3.12')
         .entrypoint([])
         .pip_install('requests>=2.31,<3')
         .env({'OLLAMA_HOST': '127.0.0.1:11434', 'OLLAMA_MODELS': MODEL_DIR,
               'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '1'})
         .add_local_python_source('app'))


def start_server():
    import requests
    process = subprocess.Popen(['ollama', 'serve'])
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('Ollama exited during startup')
        try:
            response = requests.get('http://127.0.0.1:11434/api/tags', timeout=2)
            response.raise_for_status()
            return process
        except requests.RequestException:
            time.sleep(1)
    process.terminate()
    raise RuntimeError('Ollama startup timed out')


@app.function(image=image, volumes={MODEL_DIR: weights}, cpu=1, memory=2048,
              max_containers=1, timeout=1800)
def prepare_model():
    """Download once without reserving a GPU. Call again to update cached weights."""
    process = start_server()
    try:
        subprocess.run(['ollama', 'pull', MODEL], check=True, timeout=1500)
        weights.commit()
        return {'model': MODEL, 'ready': True}
    finally:
        process.terminate()
        process.wait(timeout=30)


@app.function(image=image, volumes={MODEL_DIR: weights}, cpu=0.25,
              memory=256, timeout=60, scaledown_window=10)
def model_status():
    # No server or GPU startup for a readiness check.
    from pathlib import Path
    weights.reload()
    name, tag = MODEL.rsplit(':', 1)
    if not (Path(MODEL_DIR) / 'manifests/registry.ollama.ai/library' / name / tag).is_file():
        raise RuntimeError('Qwen weights are missing. Run prepare_model first.')
    return {'model': MODEL, 'ready': True}


@app.function(image=image, volumes={MODEL_DIR: weights}, cpu=1, memory=2048,
              max_containers=1, timeout=180, scaledown_window=10)
def remove_old_model():
    """Remove the retired Qwen 3 model only after the replacement is installed."""
    from app.ollama_client import OllamaClient
    from pathlib import Path
    def stored_bytes():
        return sum(path.stat().st_size for path in Path(MODEL_DIR).rglob('*') if path.is_file())
    process = start_server()
    try:
        client = OllamaClient(base_url='http://127.0.0.1:11434', model=MODEL)
        installed = client.health_check()
        if MODEL not in installed:
            raise RuntimeError('Install the replacement before removing the old model')
        before = stored_bytes()
        if 'qwen3:4b' in installed:
            subprocess.run(['ollama', 'rm', 'qwen3:4b'], check=True, timeout=90)
        weights.commit()
        return {'model': MODEL, 'installed_models': client.health_check(),
                'freed_bytes': max(0, before - stored_bytes())}
    finally:
        process.terminate()
        process.wait(timeout=30)


@app.cls(image=image, volumes={MODEL_DIR: weights}, gpu='T4', cpu=1,
         memory=4096, min_containers=0, max_containers=1,
         scaledown_window=60, timeout=420)
class Qwen:
    @modal.enter()
    def load(self):
        from app.ollama_client import OllamaClient
        self.process = start_server()
        self.client = OllamaClient(base_url='http://127.0.0.1:11434', model=MODEL)
        if MODEL not in self.client.health_check():
            raise RuntimeError('Qwen weights are missing. Run prepare_model first.')

    @modal.method()
    def chat(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        if not messages or len(messages) > 100:
            raise ValueError('Expected between 1 and 100 messages')
        return self.client._chat(messages, json_mode=json_mode)

    @modal.exit()
    def stop(self):
        if hasattr(self, 'process'):
            self.process.terminate()
