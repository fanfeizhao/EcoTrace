"""Compatibility entry point for the repaired walking app."""
from pathlib import Path
from ecotrace_repaired import run
if __name__=='__main__':run(Path(__file__).resolve().parent)
