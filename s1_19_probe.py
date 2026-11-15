"""Temporary S1-19 static-analysis probe; never run or merge this app."""

import subprocess

from fastapi import FastAPI

app = FastAPI()


@app.get("/s1-19-probe")
def probe(command: str) -> str:
    return subprocess.check_output(command, shell=True, text=True)
