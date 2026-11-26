"""Public, read-only prototype MCP endpoint; explicitly authorized for no sign-in.

Deploy: python -m modal deploy modal_mcp.py
Connect the deployed HTTPS URL plus /mcp from ChatGPT or Claude.
This app never mounts the SQLite Volume and does not invoke Qwen.
It reads the existing worker's production dataset by default; no sample database
is created. OAuth can be configured later by changing MCP_AUTH_MODE and attaching
the landlord-mcp Secret with the OAuth settings.
"""

import modal


app = modal.App('landlord-admin-mcp')
image = (modal.Image.debian_slim(python_version='3.12')
         .pip_install('mcp==2.3.0', 'PyJWT[crypto]==2.15.1')
         .env({'MCP_AUTH_MODE': 'none', 'MODAL_WORKER_APP': 'landlord-db-worker-test',
               'MODAL_DATASET': 'production'})
         .add_local_python_source('app'))


@app.function(image=image,
              cpu=0.25, memory=512, min_containers=0, max_containers=1,
              scaledown_window=5, timeout=120)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def server():
    from app.mcp_server import create_mcp_app
    url = modal.Function.from_name('landlord-admin-mcp', 'server').get_web_url()
    return create_mcp_app(public_url=url.rstrip('/') + '/mcp')
