# Run with: modal-jobs uv run -s GREETING=hello -s TARGET=modal examples/04_uv_secret.py
#
# To use a secret stored in Modal instead, pass its name:
#   modal secret create my-secret GREETING=hello TARGET=modal
#   modal-jobs uv run -s my-secret examples/04_uv_secret.py
import os

print(f"{os.environ['GREETING']}, {os.environ['TARGET']}!")
