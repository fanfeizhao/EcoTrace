"""Start the existing repaired EcoTrace app with local GIS data enabled."""
import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,default=Path.home()/'Downloads/EcoTrace')
    parser.add_argument('--port',type=int,default=8093)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    root=args.project.resolve()
    sys.path.insert(0,str(root))
    # Prefer the runtime shipped beside this launcher over any older copied helper.
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from ecotrace_gis_runtime import activate, augment_source, VERSION
    print('Starting '+VERSION+' from '+str(Path(__file__).resolve().parent),flush=True)
    activate(root)
    import ecotrace_repaired
    original=ecotrace_repaired.build_source
    def build_source(project):
        return augment_source(original(project))
    ecotrace_repaired.build_source=build_source
    def verify():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url=f'http://127.0.0.1:{args.port}/api/ecotrace-gis-status'
        for _ in range(180):
            try:
                with opener.open(url,timeout=2) as response:
                    data=json.load(response)
                if data.get('version')==VERSION and data.get('active'):
                    print('VERIFIED: '+VERSION+' is running. '+url,flush=True)
                    print('After comparing routes, open that link and check sample_calls and canopy_score_increases.',flush=True)
                    return
            except (OSError,ValueError):
                pass
            time.sleep(.5)
        print('GIS startup could not be verified; check the app error above.',flush=True)
    threading.Thread(target=verify,daemon=True).start()
    ecotrace_repaired.run(root,args.port,not args.no_browser)


if __name__=='__main__':
    main()
