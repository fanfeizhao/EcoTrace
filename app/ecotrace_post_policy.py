"""Account ownership for community and e-waste mutations; guests may discuss/report."""
import importlib,json,re,sqlite3
from pathlib import Path
from urllib.parse import parse_qsl,urlencode
from fastapi import Request,HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel,Field
class ListingEdit(BaseModel):
    title:str=Field(min_length=3,max_length=120)
    description:str=Field(min_length=3,max_length=1500)
    asking_price:float|None=Field(default=None,ge=0,le=100000,allow_inf_nan=False)
def install_post_policy(app,current_username,auth_db_path):
    db=Path(auth_db_path).resolve().parent/'ecotrace_post_owners.db'
    def connect():
        c=sqlite3.connect(db,timeout=10);c.row_factory=sqlite3.Row;return c
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS owners(resource TEXT,item_id INTEGER,username TEXT NOT NULL,edit_token TEXT,PRIMARY KEY(resource,item_id))')
        c.execute('CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT,resource TEXT,item_id INTEGER,username TEXT,reason TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    def owner(resource,item_id):
        with connect() as c:return c.execute('SELECT * FROM owners WHERE resource=? AND item_id=?',(resource,item_id)).fetchone()
    def remember(resource,item_id,user,token=None):
        with connect() as c:c.execute('INSERT OR IGNORE INTO owners VALUES(?,?,?,?)',(resource,item_id,user,token))
    def category(post_id):
        mod=importlib.import_module('community_hub_v6_preserve_posts')
        with mod._connect() as c:row=c.execute('SELECT category FROM community_posts WHERE id=?',(post_id,)).fetchone()
        return row['category'] if row else None
    def fail(status,message):return JSONResponse({'detail':message},status_code=status)
    @app.middleware('http')
    async def post_permissions(request:Request,call_next):
        path=request.url.path.rstrip('/');match=re.fullmatch(r'/api/(community|ewaste)/(posts|comments|listings|events)(?:/(\d+))?(?:/(comments|replies|report|complete|join|leave))?',path)
        if not match:return await call_next(request)
        group,kind,raw_id,action=match.groups();item_id=int(raw_id) if raw_id else None;resource=group+'/'+kind;user=current_username(request);method=request.method;mutating=method not in ['GET','HEAD','OPTIONS'];legacy=False;legacy_token=None
        if mutating and action!='report':
            guest_chat=method=='POST' and group=='community' and kind=='posts' and action=='comments' and category(item_id) in ['discussion']
            if not user and not guest_chat:return fail(401,'Sign in to create or change posts, join events, or send e-waste messages.')
            own_action=(method in ['PATCH','PUT','DELETE'] and action not in ['join','leave']) or action=='complete'
            query=dict(parse_qsl(request.scope.get('query_string',b'').decode()))
            if own_action:
                record=owner(resource,item_id)
                if record and record['username']!=user:return fail(403,'Only the signed-in author can change this item.')
                if not record:
                    legacy_token=query.get('edit_token','')
                    if not legacy_token or resource not in ['community/posts','community/comments','ewaste/events']:return fail(403,'This older item has no verified account owner. It remains available to read.')
                    legacy=True
                elif record['edit_token']:query['edit_token']=record['edit_token']
            if user and action in ['join','leave'] and 'display_name' in query:query['display_name']=user
            request.scope['query_string']=urlencode(query).encode()
            if method in ['POST','PATCH','PUT']:
                try:body=await request.json()
                except Exception:body=None
                if isinstance(body,dict):
                    if user:
                        for key in ['display_name','host_name']:
                            if key in body:body[key]=str(user)[:40]
                    elif guest_chat:body['display_name']=('Guest: '+str(body.get('display_name') or 'Guest'))[:40]
                    if group=='ewaste' and action=='replies' and body.get('reply_type')=='seller_reply':
                        parent=owner(resource,item_id)
                        if not parent or parent['username']!=user:return fail(403,'Only the listing author can post a seller reply.')
                    request._body=json.dumps(body).encode()
        response=await call_next(request)
        if 'application/json' not in response.headers.get('content-type',''):return response
        raw=b''.join([chunk async for chunk in response.body_iterator])
        try:data=json.loads(raw)
        except ValueError:return fail(502,'Unexpected post response.')
        if 200<=response.status_code<300:
            if user and method=='POST' and action in [None,'comments','replies'] and isinstance(data,dict) and 'id' in data:
                created='community/comments' if action=='comments' else 'ewaste/replies' if action=='replies' else resource;remember(created,data['id'],user,data.get('edit_token'))
            if legacy and user:remember(resource,item_id,user,legacy_token)
            enriched='community/comments' if action=='comments' else 'ewaste/replies' if action=='replies' else resource
            for item in data if isinstance(data,list) else [data]:
                if not isinstance(item,dict) or 'id' not in item:continue
                o=owner(enriched,item['id']);item['can_edit']=bool(user and o and o['username']==user);item['legacy_author_token']=o is None and enriched in ['community/posts','community/comments','ewaste/events']
        return JSONResponse(data,status_code=response.status_code,headers={k:v for k,v in response.headers.items() if k.lower() not in ['content-length','content-type','content-encoding']})
    def require_owner(request,item_id):
        user=current_username(request)
        if not user:raise HTTPException(401,'Sign in first.')
        o=owner('ewaste/listings',item_id)
        if not o or o['username']!=user:raise HTTPException(403,'Only the signed-in listing author can change this item.')
    async def edit_listing(item_id:int,payload:ListingEdit,request:Request):
        require_owner(request,item_id);mod=importlib.import_module('ewaste_backend_v7')
        with mod._connect() as c:
            cursor=c.execute('UPDATE ewaste_listings SET title=?,description=?,asking_price=? WHERE id=?',(payload.title.strip(),payload.description.strip(),payload.asking_price,item_id))
            if cursor.rowcount!=1:raise HTTPException(404,'Listing not found.')
        return {'id':item_id,'updated':True}
    async def delete_listing(item_id:int,request:Request):
        require_owner(request,item_id);mod=importlib.import_module('ewaste_backend_v7')
        with mod._connect() as c:
            if not c.execute('SELECT id FROM ewaste_listings WHERE id=?',(item_id,)).fetchone():raise HTTPException(404,'Listing not found.')
            c.execute('DELETE FROM ewaste_listing_replies WHERE listing_id=?',(item_id,));c.execute('DELETE FROM ewaste_listings WHERE id=?',(item_id,))
        return {'id':item_id,'deleted':True}
    async def report_listing(item_id:int,request:Request):
        try:body=await request.json()
        except Exception:body={}
        reason=str(body.get('reason') or '').strip()
        if not 3<=len(reason)<=1000:raise HTTPException(422,'Enter a reason between 3 and 1000 characters.')
        mod=importlib.import_module('ewaste_backend_v7')
        with mod._connect() as c:
            if not c.execute('SELECT id FROM ewaste_listings WHERE id=?',(item_id,)).fetchone():raise HTTPException(404,'Listing not found.')
        with connect() as c:c.execute('INSERT INTO reports(resource,item_id,username,reason) VALUES(?,?,?,?)',('ewaste/listings',item_id,current_username(request),reason))
        return {'reported':True,'message':'Report recorded for review. The listing stays visible.'}
    for method,endpoint,suffix in [('PATCH',edit_listing,''),('DELETE',delete_listing,''),('POST',report_listing,'/report')]:
        app.add_api_route('/api/ewaste/listings/{item_id}'+suffix,endpoint,methods=[method]);app.router.routes.insert(0,app.router.routes.pop())
