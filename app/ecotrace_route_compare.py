"""Keep distinct walking alternatives and explain provider/filter outcomes."""
import ast,bisect,math,re
MARKER='# ECOTRACE_ROUTE_COMPARE_V4'

def _distance(a,b):
    p,q=math.radians(a[1]),math.radians(b[1])
    h=math.sin((q-p)/2)**2+math.cos(p)*math.cos(q)*math.sin(math.radians(b[0]-a[0])/2)**2
    return 12742000*math.asin(min(1,math.sqrt(max(0,h))))

def same_path(a,b):
    """Compare geometry in the same travel direction, with a 2 m tolerance."""
    a=a.get('coordinates',[]);b=b.get('coordinates',[])
    if len(a)<2 or len(b)<2:return False
    if a==b:return True
    def distances(points):
        values=[0.0]
        for left,right in zip(points,points[1:]):values.append(values[-1]+_distance(left,right))
        return values
    da,db=distances(a),distances(b)
    if abs(da[-1]-db[-1])>2 or min(da[-1],db[-1])<=0:return False
    if _distance(a[0],b[0])>2 or _distance(a[-1],b[-1])>2:return False
    count=max(2,min(4000,math.ceil(max(da[-1],db[-1])/10)))
    def sample(points,distances,fraction):
        target=distances[-1]*fraction
        i=min(len(points)-1,max(1,bisect.bisect_left(distances,target)))
        span=distances[i]-distances[i-1]
        w=(target-distances[i-1])/span if span else 0
        return [points[i-1][k]+w*(points[i][k]-points[i-1][k]) for k in (0,1)]
    return all(_distance(sample(a,da,i/count),sample(b,db,i/count))<=2 for i in range(count+1))

def collect_candidates(fetch,api_key,start,end):
    initial=fetch(api_key,start,end,include_alternatives=False)
    if not initial:raise ValueError('OpenRouteService returned no decodable walking route.')
    candidates=[]
    for route in initial:
        if not any(same_path(route,old) for old in candidates):candidates.append(route)
    diagnostic={'baseline_returned':len(initial),'alternatives_returned':0,'request_status':'ok'}
    try:
        additional=fetch(api_key,start,end,include_alternatives=True)
        diagnostic['alternatives_returned']=len(additional)
        for route in additional:
            if not any(same_path(route,old) for old in candidates):candidates.append(route)
    except (RuntimeError,ValueError) as error:
        diagnostic['request_status']='failed'
        # Do not send provider exceptions or credentials to the public response.
        print('EcoTrace alternative request failed ('+type(error).__name__+').',flush=True)
    diagnostic['unique_routes']=len(candidates)
    return candidates,diagnostic

def comparison_message(diagnostic,has_alternative,gain):
    count=diagnostic['unique_routes'];eligible=diagnostic['eligible_routes']
    if has_alternative:
        label='Shadier alternative' if gain>=2 else 'Similar-shade alternative' if gain>-2 else 'Less-shaded alternative'
        shade_note=(f'The green alternative gains {gain:.1f} modeled shade-score points.' if gain>=2 else
                    'The two routes have similar modeled shade.' if gain>-2 else
                    f'The shortest route has {-gain:.1f} more modeled shade-score points.')
        text=f'{count} distinct routes found; {eligible} within your detour limit. '+shade_note
        if diagnostic['request_status']=='failed':text+=' The additional-alternatives request failed; this comparison uses the routes already returned.'
        return label,text
    if count>1:
        return 'Alternative route',f'{count} distinct routes found, but every alternative exceeds your detour limit. Increase the extra-distance allowance to compare them.'
    if diagnostic['request_status']=='failed':
        return 'Alternative route','The shortest route loaded, but the additional-route request failed. Whether a shadier alternative exists has not been established.'
    if diagnostic['request_status']=='not_reported':
        return 'Alternative route','Routing supplied only one distinct path for comparison. Whether a shadier alternative exists has not been established.'
    return 'Alternative route','OpenRouteService returned only one distinct path. EcoTrace could not compare shade between different routes for this request.'

def compare_returned_candidates(routes):
    """Preserve the installed routing helper and compare the paths it returns.

    This helper does not expose per-request status or pre-filter candidate counts.
    Do not infer successful alternative retrieval from a returned baseline.
    """
    if not routes:
        raise ValueError('Routing returned no decodable walking route.')
    candidates=[]
    for route in routes:
        if not any(same_path(route,old) for old in candidates):
            candidates.append(route)
    return candidates, {'candidates_received':len(routes),'unique_routes':len(candidates),
                        'request_status':'not_reported','request_source':'installed_route_helper'}

def finish_comparison(result):
    if getattr(result,'nighttime',False):
        result.alternative_route_label='Alternative route'
        result.meaningful_shadier_route=False
        if result.has_alternative_route:
            result.message='Two routes are shown for distance and travel-time comparison. Direct sunlight is absent at the selected departure time.'
        elif result.route_diagnostics:
            _,result.message=comparison_message(result.route_diagnostics,False,0)
    return result

def once(source,old,new):
    if source.count(old)!=1:raise ValueError('Route comparison anchor differs; no files changed: '+old[:70])
    return source.replace(old,new,1)

def transform_route_function(block):
    """Edit Python statements, independently of comments and line wrapping."""
    function = ast.parse(block).body[0]
    def assigned(node, name):
        return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    def calls(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]
    def require_one(items, label):
        if len(items) != 1:
            raise ValueError('Route update cannot identify ' + label + ' in ors_routes_analysis; no files changed.')
        return items[0]
    def statements(text):
        return ast.parse(text).body
    helper_requests = [n for n in function.body if isinstance(n,ast.Assign) and calls(n,'fetch_route_candidates')]
    if helper_requests:
        initial = require_one(helper_requests, 'the installed route helper request')
        if len(initial.targets)!=1 or ast.unparse(initial.targets[0])!='(candidates, routing_note)':
            raise ValueError('Unsupported route-helper return variables; no files changed.')
        index = function.body.index(initial)+1
        function.body[index:index] = statements('candidates, route_diagnostics = compare_returned_candidates(candidates)')
    else:
        initial = require_one([n for n in function.body if assigned(n, 'candidates') and calls(n, '_request_ors_route')], 'the initial route request')
        optional = require_one([n for n in function.body if isinstance(n, ast.Try) and calls(n, '_request_ors_route')], 'the optional alternatives request')
        begin, end = function.body.index(initial), function.body.index(optional)
        if begin >= end:
            raise ValueError('Route requests are in an unsupported order; no files changed.')
        # Retain other setup, including departure parsing if it appears here.
        retained = []
        for node in function.body[begin + 1:end]:
            empty_guard = (isinstance(node, ast.If) and ast.unparse(node.test) == 'not candidates')
            if assigned(node, 'baseline') or empty_guard:
                continue
            retained.append(node)
        function.body[begin:end + 1] = statements('candidates, route_diagnostics = collect_candidates(_request_ors_route, api_key, start, end)') + retained
    choice = require_one([n for n in function.body if assigned(n, 'shadiest')], 'alternative selection')
    if not (isinstance(choice.value, ast.Call) and isinstance(choice.value.func, ast.Name) and choice.value.func.id == 'max' and choice.value.args and isinstance(choice.value.args[0], ast.Name) and choice.value.args[0].id == 'allowed'):
        raise ValueError('Unsupported alternative selection expression; no files changed.')
    choice.value.args[0] = ast.parse('distinct_alternatives or [baseline]', mode='eval').body
    index = function.body.index(choice)
    function.body[index:index] = statements('distinct_alternatives = [item for item in allowed if not same_path(item, baseline)]\nhas_alternative_route = bool(distinct_alternatives)\nroute_diagnostics["eligible_routes"] = len(allowed)\nroute_diagnostics["excluded_by_detour"] = len(candidates) - len(allowed)')
    meaningful = require_one([n for n in function.body if assigned(n, 'meaningful_shadier_route')], 'the shade-improvement decision')
    meaningful.value = ast.parse('has_alternative_route and shade_gain_raw >= 2.0', mode='eval').body
    message = require_one([n for n in function.body if assigned(n, 'route_message')], 'the route comparison message')
    function.body[function.body.index(message)] = statements('alternative_route_label, route_message = comparison_message(route_diagnostics, has_alternative_route, shade_gain_raw)')[0]
    for node in list(function.body):
        if isinstance(node, ast.If) and ast.unparse(node.test) == 'not meaningful_shadier_route':
            if node.orelse or not all(assigned(child, 'route_message') for child in node.body):
                raise ValueError('Unsupported hidden-alternative handling; no files changed.')
            function.body.remove(node)
    result = require_one([n for n in function.body if isinstance(n, ast.Return)], 'the final route response')
    response = require_one(calls(result, 'RouteAnalysis'), 'the RouteAnalysis response')
    for name in ('has_alternative_route', 'alternative_route_label', 'route_diagnostics'):
        response.keywords.append(ast.keyword(arg=name, value=ast.Name(id=name, ctx=ast.Load())))
    result.value = ast.Call(func=ast.Name(id='finish_comparison', ctx=ast.Load()), args=[result.value], keywords=[])
    class Labels(ast.NodeTransformer):
        def visit_Constant(self, node):
            if node.value == 'The shade-oriented route':
                return ast.copy_location(ast.Name(id='alternative_route_label', ctx=ast.Load()), node)
            return node
    function = Labels().visit(function)
    ast.fix_missing_locations(function)
    return ast.unparse(function) + '\n'

def transform(source):
    if MARKER in source:return source
    source=once(source,'import sqlite3','import sqlite3\nfrom ecotrace_route_compare import collect_candidates, compare_returned_candidates, same_path, comparison_message, finish_comparison')
    source=once(source,'class RouteAnalysis(BaseModel):','class RouteAnalysis(BaseModel):\n    has_alternative_route: bool = False\n    alternative_route_label: str = "Alternative route"\n    route_diagnostics: dict[str, Any] | None = None')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='ors_routes_analysis')
    lines=source.splitlines(keepends=True);block=''.join(lines[node.lineno-1:node.end_lineno])
    block = transform_route_function(block)

    source=''.join(lines[:node.lineno-1])+block+'\n'+''.join(lines[node.end_lineno:])
    # The renderer now separates route availability from a meaningful shade gain.
    start_match=re.search(r'async\s+function\s+requestRoute\s*\(\s*\)\s*\{',source)
    if not start_match:raise ValueError('Route renderer not found; no files changed.')
    start=start_match.start()
    end_match=re.search(r'async\s+function\s+loadAssistantPrompts\s*\(\s*\)\s*\{',source[start_match.end():])
    if not end_match:raise ValueError('End of route renderer not found; no files changed.')
    end=start_match.end()+end_match.start()
    ui=source[start:end].replace('data.meaningful_shadier_route','data.has_alternative_route')
    ui=ui.replace('● Shade-oriented route','● ${escapeHtml(data.alternative_route_label)}')
    ui=ui.replace("label:'Shadier alternative'","label:data.alternative_route_label")
    ui=ui.replace('No meaningfully shadier alternative found.','One route available for comparison.')
    ui=ui.replace("The available alternatives were the same route or improved the estimated shade score by less than 2 points within this profile's detour limit.",'${escapeHtml(data.message)}')
    ui=ui.replace("'No meaningfully shadier route found — shortest route shown'","'One route shown — see route comparison details'")
    ui=ui.replace('+${data.shade_gain_percent}',"${data.shade_gain_percent>0?'+':''}${data.shade_gain_percent}")
    ui=ui.replace('Shade gain</p>','Shade-score difference</p>')
    ui=ui.replace('${data.has_alternative_route?`\n    <div class="mt-3 grid grid-cols-3', '${data.has_alternative_route&&!data.nighttime?`\n    <div class="mt-3 grid grid-cols-3')
    source=source[:start]+ui+source[end:]
    source+='\n'+MARKER+'\n'
    compile(source,'EcoTrace route comparison','exec')
    return source
