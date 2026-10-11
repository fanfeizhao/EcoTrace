from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
entry = root / 'editor' / 'start_review.py'
if not entry.exists():
    raise SystemExit('V19 editor source was not exported. See export_report.json.')
sys.path.insert(0, str(entry.parent))
sys.argv = [str(entry), '--project', str(root / 'app'), '--gis-folder', str(root / 'gis'), *sys.argv[1:]]
runpy.run_path(str(entry), run_name='__main__')
