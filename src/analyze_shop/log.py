"""Log có màu ANSI (Docker Desktop / Windows Terminal đều hiển thị được)."""
from datetime import datetime


class C:
    INFO      = "\033[94m"
    ACTION    = "\033[94m"
    WARN      = "\033[93m"
    CKPT      = "\033[92m"
    ERROR     = "\033[91m"
    INTERRUPT = "\033[95m"
    TIME      = "\033[96m"
    DONE      = "\033[92m"
    RESET     = "\033[0m"

    @staticmethod
    def tag(color: str, label: str) -> str:
        return f"{color}[{label}]{C.RESET}"


def ts() -> str:
    return C.tag(C.TIME, datetime.now().strftime("%H:%M:%S"))

def info(msg):  print(f"{ts()} {C.tag(C.INFO,      'INFO')}  {msg}", flush=True)
def warn(msg):  print(f"{ts()} {C.tag(C.WARN,      'WARN')}  {msg}", flush=True)
def ckpt(msg):  print(f"{ts()} {C.tag(C.CKPT,      'CKPT')}  {msg}", flush=True)
def err(msg):   print(f"{ts()} {C.tag(C.ERROR,     'ERROR')} {msg}", flush=True)
def done(msg):  print(f"{ts()} {C.tag(C.DONE,      'DONE')}  {msg}", flush=True)
def stop(msg):  print(f"{ts()} {C.tag(C.INTERRUPT, 'STOP')}  {msg}", flush=True)
