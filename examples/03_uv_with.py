# Run with: modal-jobs uv run --with rich --with "requests>=2,<3" examples/03_uv_with.py
import requests
from rich.pretty import pprint

resp = requests.get("https://peps.python.org/api/peps.json")
data = resp.json()
pprint([(k, v["title"]) for k, v in data.items()][:10])
