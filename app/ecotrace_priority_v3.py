"""Start EcoTrace with the optional priority extension; original source stays intact."""
from pathlib import Path
import os
from ecotrace_priority_runtime import validate,transform,install_hooks
root=Path(__file__).resolve().parent
validate(root)
main=root/'ecotrace_final_input_polish_v6149.py'
source=transform(main.read_text())
install_hooks()
os.chdir(root)
# Use the registered __main__ namespace so Pydantic resolves nested model annotations.
# A detached dict leaves models such as ZoneAnalysis incomplete at request time.
globals()['__file__']=str(main)
exec(compile(source,str(main),'exec'),globals())
