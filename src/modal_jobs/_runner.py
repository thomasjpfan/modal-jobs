from subprocess import run


def run_cmd(cmd):
    run(cmd, check=True)
