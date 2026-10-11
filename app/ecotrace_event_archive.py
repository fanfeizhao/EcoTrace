"""Date-based event archives. Original records and completion status are preserved."""
from __future__ import annotations
import asyncio
from datetime import datetime,time,timedelta,timezone
import json
import re
from zoneinfo import ZoneInfo

VERSION='auto-archive-v3'
MARKER='# ECOTRACE_AUTO_ARCHIVE_V2'
PACIFIC=ZoneInfo('America/Los_Angeles')
EVENT_CATEGORIES={'park_cleanup','environmental_campaign'}
DURATIONS={'About 30 minutes':30,'About 1 hour':60,'About 2 hours':120,'About 3 hours':180}

def parse_activity_date(raw):
    if not isinstance(raw,str) or not raw.strip():return None
    raw=' '.join(raw.strip().split())
    try:return datetime.fromisoformat(raw.replace('Z','+00:00'))
    except ValueError:pass
    # Existing records can contain the US display format as well as ISO form dates.
    formats=('%m/%d/%Y, %I:%M:%S %p','%m/%d/%Y, %I:%M %p',
        '%m/%d/%Y %I:%M:%S %p','%m/%d/%Y %I:%M %p',
        '%m/%d/%Y %H:%M:%S','%m/%d/%Y %H:%M','%m/%d/%Y',
        '%B %d, %Y','%b %d, %Y','%d %B %Y','%d %b %Y')
    for fmt in formats:
        try:return datetime.strptime(raw,fmt)
        except ValueError:continue
    return None

def event_cutoff(record):
    """Known elapsed duration, otherwise the next Pacific midnight. Invalid = unknown."""
    raw=record.get('event_date')
    if not isinstance(raw,str) or not raw.strip():return None
    raw=raw.strip()
    try:
        start=parse_activity_date(raw)
        if start is None:return None
        # HTML datetime-local dates are entered for the local CA-17 event.
        # fold=1 keeps ambiguous fall-back times active through their later instance.
        if start.tzinfo is None:start=start.replace(tzinfo=PACIFIC,fold=1)
        start=start.astimezone(PACIFIC)
        duration=DURATIONS.get(record.get('duration_text'))
        if duration is not None and re.search(r'\d{1,2}:\d{2}',raw):
            return start.astimezone(timezone.utc)+timedelta(minutes=duration)
        return datetime.combine(start.date()+timedelta(days=1),time.min,tzinfo=PACIFIC).astimezone(timezone.utc)
    except (ValueError,TypeError,OverflowError):return None

def is_event(record,kind):
    return isinstance(record,dict) and 'event_date' in record and 'event_status' in record and (
        kind=='ewaste' or record.get('category') in EVENT_CATEGORIES)

def past_event(record,now=None):
    cutoff=event_cutoff(record)
    now=now or datetime.now(timezone.utc)
    return cutoff is not None and now.astimezone(timezone.utc)>=cutoff

def annotate(payload,kind,now=None):
    """Derived archive status survives restarts because saved dates are authoritative."""
    if isinstance(payload,list):return [annotate(v,kind,now) for v in payload]
    if not is_event(payload,kind):return payload
    result=dict(payload)
    if result.get('event_status') in {'open','full','archived'} and past_event(result,now):
        result['original_event_status']=result['event_status']
        result['event_status']='archived'
        result['auto_archived']=True
        result['archive_reason']='Past scheduled date/time'
    return result

class EventArchiveMiddleware:
    def __init__(self,app,community,ewaste,clock=None):
        self.app=app;self.modules={'community':community,'ewaste':ewaste}
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def closed_event(self,kind,identity):
        module=self.modules[kind]
        table='community_posts' if kind=='community' else 'ewaste_events'
        # Table names are internal constants; IDs are bound parameters.
        with module._DB_LOCK:
            connection=module._connect()
            try:
                row=connection.execute(f'SELECT * FROM {table} WHERE id = ?',(identity,)).fetchone()
            finally:connection.close()
        if row is None:return False
        record=dict(row)
        if not is_event(record,kind):return False
        return record.get('event_status') in {'completed','cancelled','archived'} or past_event(record,self.clock())

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope.get('path','')
        community=path=='/api/community/posts' or path.startswith('/api/community/posts/')
        ewaste=path=='/api/ewaste/events' or path.startswith('/api/ewaste/events/')
        if not (community or ewaste):return await self.app(scope,receive,send)
        kind='community' if community else 'ewaste'
        join=re.fullmatch(r'/api/(community/posts|ewaste/events)/(\d+)/join',path)
        if join and scope.get('method')=='POST' and await asyncio.to_thread(self.closed_event,kind,int(join.group(2))):
            body=json.dumps({'detail':'This event is archived or closed and is no longer open for joining.'}).encode()
            await send({'type':'http.response.start','status':409,'headers':[(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())]})
            return await send({'type':'http.response.body','body':body})
        # List/detail JSON only. Existing create/edit/completion responses keep their contract.
        if scope.get('method')!='GET':return await self.app(scope,receive,send)
        start=None;parts=[];rewrite=False
        async def wrapped_send(message):
            nonlocal start,rewrite
            if message['type']=='http.response.start':
                headers=dict(message.get('headers',[]))
                rewrite=message['status']==200 and b'application/json' in headers.get(b'content-type',b'') and b'content-encoding' not in headers
                if not rewrite:return await send(message)
                start=dict(message);return
            if message['type']!='http.response.body' or not rewrite:return await send(message)
            parts.append(message.get('body',b''))
            if message.get('more_body'):return
            original=b''.join(parts)
            try:body=json.dumps(annotate(json.loads(original),kind,self.clock()),ensure_ascii=False,allow_nan=False).encode()
            except (ValueError,TypeError):body=original
            headers=[(k,v) for k,v in start.get('headers',[]) if k.lower() not in {b'content-length',b'etag',b'cache-control'}]
            start['headers']=headers+[(b'content-length',str(len(body)).encode()),(b'cache-control',b'no-store')]
            await send(start);await send({'type':'http.response.body','body':body})
        await self.app(scope,receive,wrapped_send)

def install(app):
    if getattr(app.state,'event_archive_enabled',False):return
    import community_hub_v6_preserve_posts as community
    import ewaste_backend_v7 as ewaste
    from types import SimpleNamespace
    def backend(module,table):
        # Older extension modules can re-export a router from a previous backend.
        spaces=[vars(module)]+[getattr(r.endpoint,'__globals__',{}) for r in module.router.routes if hasattr(r,'endpoint')]
        for space in spaces:
            connect=space.get('_connect');lock=space.get('_DB_LOCK')
            if not callable(connect) or lock is None:continue
            with lock:
                connection=connect()
                try:columns={row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                finally:connection.close()
            if {'id','event_date','event_status'}.issubset(columns):
                return SimpleNamespace(_connect=connect,_DB_LOCK=lock)
        raise RuntimeError('Cannot locate the installed event database for '+table)
    app.add_middleware(EventArchiveMiddleware,community=backend(community,'community_posts'),ewaste=backend(ewaste,'ewaste_events'))
    app.state.event_archive_enabled=True
    @app.get('/api/event-archive-status')
    async def archive_status():
        return {'enabled':True,'version':VERSION,'timezone':'America/Los_Angeles',
                'current_date':datetime.now(PACIFIC).date().isoformat()}
    print('EcoTrace automatic event archiving ENABLED (v3; Pacific date).',flush=True)

def _transform_ui_v2(source):
    if MARKER in source:return source
    # Only event-list predicates are replaced. Completion/impact bookkeeping is unchanged.
    patterns=[
        (r"post\.event_status!=='completed'\s*&&\s*post\.event_status!=='cancelled'","!ecoEventArchived(post)",1),
        (r"post\.event_status==='completed'\|\|post\.event_status==='cancelled'","ecoEventArchived(post)",2),
        (r"event\.event_status!=='completed'\s*&&\s*event\.event_status!=='cancelled'","!ecoEventArchived(event)",1),
        (r"event\.event_status==='completed'\|\|event\.event_status==='cancelled'","ecoEventArchived(event)",1),
    ]
    already_patched='function ecoEventArchived(event)' in source
    if not already_patched:
        for pattern,replacement,expected in patterns:
            source,count=re.subn(pattern,replacement,source)
            if count!=expected:raise ValueError('Event archive UI differs from supported version; no files changed.')
    source=source.replace('text-slate-300">Done</span>`:\'\'}','text-slate-300">${post.auto_archived?\'Past date\':post.event_status===\'cancelled\'?\'Cancelled\':\'Done\'}</span>`:\'\'}',1)
    old="${archived?'<span class=\"rounded-full bg-slate-400/10 px-2 py-1 text-[9px] font-bold text-slate-300\">Done</span>':''}"
    new="${archived?`<span class=\"rounded-full bg-slate-400/10 px-2 py-1 text-[9px] font-bold text-slate-300\">${event.auto_archived?'Past date':event.event_status==='cancelled'?'Cancelled':'Done'}</span>`:''}"
    if not already_patched:
        if source.count(old)!=1:raise ValueError('E-waste archive badge not found.')
        source=source.replace(old,new,1)
    helper=DATE_HELPERS+"""
let ecoArchiveRefreshing=false;
async function ecoRefreshEventArchives(){
  if(document.hidden||ecoArchiveRefreshing)return;
  ecoArchiveRefreshing=true;
  try{
    if(document.getElementById('community-post-list')?.offsetParent && ['events','archive'].includes(activeCommunityTab))await loadCommunityPosts();
    if(document.getElementById('ewaste-event-list')?.offsetParent)await loadEwasteEvents();
    if(document.getElementById('ewaste-archive-list')?.offsetParent)await loadEwasteArchive();
  }finally{ecoArchiveRefreshing=false;}
}
setInterval(()=>{ecoRefreshEventArchives().catch(()=>{});},60000);
document.addEventListener('visibilitychange',()=>{ecoRefreshEventArchives().catch(()=>{});});
"""
    marker='async function loadCommunityPosts(){'
    if source.count(marker)!=1:raise ValueError('Community list loader not found.')
    if already_patched:
        start=source.index('function ecoEventArchived(event)')
        end=source.index(marker,start)
        source=source[:start]+helper+'\n'+source[end:]
    else:
        source=source.replace(marker,helper+'\n'+marker,1)
    source=source.replace("post.auto_archived?'Past date'","ecoEventPastDate(post)?'Past date'")
    source=source.replace("event.auto_archived?'Past date'","ecoEventPastDate(event)?'Past date'")
    source=source.replace('Review completed park cleanups and environmental campaigns.','Review past, completed, and cancelled environmental events.')
    source=source.replace('Events marked Done are moved here instead of deleted.','Past events appear here automatically. Dates use Pacific time; events with no clear end time archive after their scheduled day.')
    source=source.replace('No completed environmental events yet.','No archived environmental events yet.')
    source+='\n'+MARKER+'\n'
    compile(source,'EcoTrace event archive UI','exec')
    return source

DATE_HELPERS=r'''
function ecoPacificDay(value){
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value);
  const get=k=>parts.find(p=>p.type===k).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function ecoCalendarDay(y,m,d){
  y=Number(y);m=Number(m);d=Number(d);
  if(y<1000||y>9999||m<1||m>12||d<1||d>31)return null;
  const test=new Date(Date.UTC(y,m-1,d));
  if(test.getUTCFullYear()!==y||test.getUTCMonth()+1!==m||test.getUTCDate()!==d)return null;
  return `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
function ecoActivityDay(raw){
  if(typeof raw!=='string')return null;
  const s=raw.trim().replace(/\s+/g,' ');
  let m;
  if(/^\d{4}-\d{2}-\d{2}[T ]/.test(s)&&/(Z|[+-]\d{2}:?\d{2})$/i.test(s)){
    const instant=new Date(s);
    return Number.isFinite(instant.getTime())?ecoPacificDay(instant):null;
  }
  m=s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?)?$/);
  if(m)return ecoCalendarDay(m[1],m[2],m[3]);
  m=s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:(?:,? )(?:\d{1,2}):\d{2}(?::\d{2})?(?: [AP]M)?)?$/i);
  if(m)return ecoCalendarDay(m[3],m[1],m[2]);
  const months=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  m=s.match(/^([A-Za-z]+) (\d{1,2}), (\d{4})$/);
  if(m)return ecoCalendarDay(m[3],months.indexOf(m[1].slice(0,3).toLowerCase())+1,m[2]);
  m=s.match(/^(\d{1,2}) ([A-Za-z]+) (\d{4})$/);
  if(m)return ecoCalendarDay(m[3],months.indexOf(m[2].slice(0,3).toLowerCase())+1,m[1]);
  return null;
}
function ecoEventPastDate(event,now=new Date()){
  if(event.auto_archived||event.event_status==='archived')return true;
  const day=ecoActivityDay(event.event_date);
  return day!==null && day<ecoPacificDay(now);
}
function ecoEventArchived(event){
  return ['completed','cancelled','archived'].includes(event.event_status)||ecoEventPastDate(event);
}
'''

FEED_HELPER=r"""
function ecoCommunityVisiblePosts(posts,tab){
  if(tab==='events')return posts.filter(p=>isCommunityEvent(p.category)&&!ecoEventArchived(p));
  if(tab==='archive')return posts.filter(p=>isCommunityEvent(p.category)&&ecoEventArchived(p));
  if(tab==='feed')return posts.filter(p=>!isCommunityEvent(p.category)||!ecoEventArchived(p));
  return posts;
}
"""
def transform_ui(source):
    if '# ECOTRACE_CURRENT_POSTS_V3' in source:return source
    source=_transform_ui_v2(source)
    pattern=r"    if\(activeCommunityTab==='events'\)\{.*?\n    \}\n\n    if\(activeCommunityTab==='feed'\)"
    source,count=re.subn(pattern,"    posts=ecoCommunityVisiblePosts(posts,activeCommunityTab);\n\n    if(activeCommunityTab==='feed')",source,count=1,flags=re.S)
    if count!=1:raise ValueError('Current-posts filter anchor was not found; nothing changed.')
    source=source.replace('async function loadCommunityPosts(){',FEED_HELPER+'\nasync function loadCommunityPosts(){',1)
    source=source.replace("['events','archive'].includes(activeCommunityTab)","['feed','events','archive'].includes(activeCommunityTab)")
    source=source.replace('escapeHtml(post.description)','escapeHtml(parseCommunityStoredDetails(post.description).base)')
    source=source.replace('description:post.description,','description:parseCommunityStoredDetails(post.description).base,')
    source=source.replace('>All Posts</button>','>Current Posts</button>')
    source=source.replace("title:'All Community Posts'","title:'Current Posts'")
    source=source.replace('Filter and sort every public environmental post.','Browse current posts and upcoming events. Past events are in Archive.')
    source=source.replace('Completed environmental events remain visible here as a record of past community activity.','Past environmental events remain here as a record of community activity.')
    source+='\n# ECOTRACE_CURRENT_POSTS_V3\n'
    compile(source,'EcoTrace current posts','exec')
    return source
