"""Start and verify the repaired EcoTrace app on the existing local app address."""
import argparse,ast,json,os,socket,sys,threading,time,urllib.request,webbrowser
from pathlib import Path

VERSION='ecotrace-repair-20260927-v4.2'

def build_source(root):
    from ecotrace_walking_comfort import prepare_source
    source=prepare_source(root)
    guards=[n for n in ast.parse(source).body if isinstance(n,ast.If) and ast.unparse(n.test)=="__name__ == '__main__'"]
    if len(guards)!=1:raise ValueError('Could not identify the app startup block.')
    # Run the existing app definitions, then start the verified app explicitly.
    lines=source.splitlines(keepends=True);node=guards[0]
    source=''.join(lines[:node.lineno-1]+['if False:  # startup handled by verified repair launcher\n']+lines[node.lineno:])
    compile(source,'repaired EcoTrace','exec')
    return source

def run(root,port=8093,open_browser=True):
    root=Path(root).resolve();os.chdir(root)
    sys.path.insert(0,str(root))
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',port))
        except OSError:raise SystemExit(f'Port {port} is already running. Stop that EcoTrace terminal with Control+C, then run this command again.')
    from ecotrace_walking_comfort import models
    try:models()
    except ImportError:pass
    source=build_source(root)
    if (root/'ecotrace_priority_v3.py').exists():
        from ecotrace_priority_runtime import install_hooks
        install_hooks()
    # Use this real module namespace so Pydantic can resolve the app's models.
    namespace=globals()
    namespace['__file__']=str(root/'ecotrace_final_input_polish_v6149.py')
    exec(compile(source,namespace['__file__'],'exec'),namespace)
    app=namespace['app']
    if not getattr(app.state,'event_archive_enabled',False):raise RuntimeError('Archive startup was not enabled.')
    if not namespace.get('ECOTRACE_WEATHER_REPAIR'):raise RuntimeError('Weather repair was not enabled.')
    @app.get('/api/ecotrace-repair-status')
    def repair_status():
        from ecotrace_event_archive import VERSION as archive_version,PACIFIC
        from ecotrace_walking_comfort import VERSION as comfort_version
        from datetime import datetime
        return {'version':VERSION,'archive_enabled':True,'archive_version':archive_version,
                'comfort_version':comfort_version,'weather_repair':True,'route_comparison_version':4,
                'current_pacific_date':datetime.now(PACIFIC).date().isoformat()}
    origin=f'http://127.0.0.1:{port}'
    def verify():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(120):
            try:
                with opener.open(origin+'/api/ecotrace-repair-status',timeout=2) as response:data=json.load(response)
                if data.get('version')!=VERSION or not data.get('archive_enabled'):
                    print('ERROR: The responding server is not this repair. Close older EcoTrace servers.',flush=True);return
                print('VERIFIED: route comparison v4, archive, and weather repair are running. Pacific date: '+data['current_pacific_date'],flush=True)
                # Read the same live event feeds used by the page and report archive results.
                for name,path in [('Environmental','/api/community/posts?section=events'),('E-waste','/api/ewaste/events')]:
                    try:
                        with opener.open(origin+path,timeout=10) as response:rows=json.load(response)
                        count=sum(bool(row.get('auto_archived')) for row in rows)
                        print(f'{name}: {count} past activities automatically archived in the loaded feed.',flush=True)
                    except Exception as exc:print(f'{name} feed check: {exc}',flush=True)
                url=origin+'/app?mode=guest&repair='+VERSION
                print('OPEN THIS PAGE: '+url,flush=True)
                if open_browser:webbrowser.open(url)
                return
            except (OSError,ValueError):time.sleep(0.5)
        print('Could not verify server startup. Review the startup error above.',flush=True)
    threading.Thread(target=verify,daemon=True).start()
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=port)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8093)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args();run(Path(__file__).resolve().parent,args.port,not args.no_browser)
