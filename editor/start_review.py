"""Launch existing EcoTrace repair + GIS + canopy editing, without modifying source."""
import argparse
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,default=Path.home()/'Downloads/EcoTrace')
    parser.add_argument('--gis-folder',type=Path,default=Path.home()/'Downloads/EcoTrace_GIS_Update')
    parser.add_argument('--port',type=int,default=8093)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--restore-all',action='store_true',help='Restore exclusions, show all saved points and export a QGIS layer')
    args=parser.parse_args()
    print('Starting EcoTrace Canopy Editor V19 — current air temperature and clean heat panel',flush=True)
    root=args.project.expanduser().resolve()
    gis=args.gis_folder.expanduser().resolve()
    for folder in (root,gis,Path(__file__).resolve().parent): sys.path.insert(0,str(folder))
    if not (root/'ecotrace_reviewed_tree_sites.json').is_file(): raise SystemExit('Candidate database missing in '+str(root))
    if args.restore_all:
        from restore_all import restore
        restore(root)
    from tree_details_live import install as install_live_details
    install_live_details(root)
    from ecotrace_gis_runtime import activate,augment_source
    import ecotrace_repaired
    activate(root)
    from priority_sync import sync
    stats=sync(root)
    if stats:print('Priority statistics rebuilt: '+str(stats['candidate_count'])+' active; '+str(stats['pending_coordinate_evidence_count'])+' need location-specific evidence.',flush=True)
    original=ecotrace_repaired.build_source
    def build(project):
        from fast_routes import transform as responsive_planting
        source=responsive_planting(augment_source(original(project)))
        from methods_update import transform as update_methods
        source=update_methods(source)
        source+='\nfrom methods_update import install_runtime as _install_methods\n_install_methods(app, Path('+repr(str(project))+'))\n'
        source+='\nfrom canopy_editor import install as _install_canopy_editor\n_install_canopy_editor(app, globals(), Path(__file__).resolve().parent)\n'
        compile(source,'EcoTrace with canopy editor','exec')
        return source
    ecotrace_repaired.build_source=build
    url=f'http://127.0.0.1:{args.port}/app?mode=guest'
    def open_when_ready():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(180):
            try:
                with opener.open(url,timeout=2) as response:
                    if response.status==200:
                        print('OPEN REVIEW: '+url,flush=True)
                        if not args.no_browser:webbrowser.open(url)
                        return
            except OSError: pass
            time.sleep(.5)
    threading.Thread(target=open_when_ready,daemon=True).start()
    ecotrace_repaired.run(root,args.port,False)

if __name__=='__main__': main()
