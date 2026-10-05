# Run with: modal-jobs uv run --gpu T4 examples/05_gpu.py
import subprocess

subprocess.run(["nvidia-smi"], check=True)
