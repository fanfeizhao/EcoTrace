"""V19: clearer heat labels, panel-scoped review link, and shared GIS methods."""
from pathlib import Path
from functools import lru_cache
import ast
import json
import re

VERSION = 'ecotrace-methods-19'
METHODS = {
    'heat_score': 'Relative heat score (0-100). Preset city heat-reference values are blended by inverse distance to the selected location. Higher means higher modeled relative heat. It is not a temperature, physiological heat index, measured exposure, or a calibrated probability.',
    'current_air_temperature': 'The Current air temperature card shows temperature_f from the available nearby NWS weather response. It shows Unavailable when that input is missing or invalid. No preset temperature is substituted.',
    'reference_temperature': 'Legacy internal model reference temperature (F), no longer displayed as the main temperature card. Preset city temperature values are blended by location. It is illustrative model context, not current air temperature, a satellite surface-temperature measurement, apparent temperature, MRT, or UTCI.',
    'route_heat': 'The fallback route heat score is the length-weighted mean of this reference heat model sampled at segment midpoints. Sun/shade minutes are calculated separately. At constant speed, length weighting equals time weighting.',
    'live_weather': 'Nearby NWS air temperature, humidity, wind and apparent temperature are separate current-weather context. NWS feels-like heat index/wind chill is separate from UTCI.',
    'comfort': 'Experimental walking MRT/UTCI uses a sunny radiation scenario plus available nearby air temperature, humidity and wind. The fallback heat score is not UTCI. Routing compares modeled shade within a detour limit; it does not optimize the displayed reference temperature.',
    'qgis_preparation': 'QGIS was used to overlay canopy on imagery, inspect alignment and pixel resolution, and clip local canopy-cover and canopy-height layers to CA-17. QGIS prepares and checks spatial data; EcoTrace performs runtime route scoring.',
    'canopy': 'Prepared 2022 San Jose and SF-Oakland canopy-cover layers and available Santa Clara and Alameda/Contra Costa height layers supplement mapped tree and building shadows. Canopy class 1 is mapped crown evidence. NoData means unknown, not open planting space. Survey years can differ.',
    'route_sampling': 'With the GIS integration, routes are split into segments at most 15 m long and sampled at segment midpoints. Segment lengths weight route averages. Canopy shadows assume flat local ground, a 1.5 m receptor, a 120 m search limit and assumed attenuation. Missing or unconfirmed heights use a labeled 8 m assumption.',
    'qgis_heat_review': 'The separately inspected August 26, 2026 surface-temperature raster is dated surface-heat evidence. Displaying it in QGIS does not replace the app heat-reference model. The separate 120-point QGIS trial was discarded and was not imported into the candidate database.',
    'candidates': 'Saved candidate records are possible locations for review, not planted trees or approved planting sites. Counts of mapped trees, canopy pixels, candidate records and displayed/filtered results are different quantities. Some candidates overlap existing crowns; nearby space requires confirmation.',
    'priority': 'The Community context percentile is descriptive. Where the separate candidate-priority module is installed, it combines dated surface-heat and tract-poverty indicators; new or relocated points can have incomplete evidence. Do not infer individual poverty, planting approval or field suitability from this planning label.',
    'gemini': 'Gemini explains compact app results and these methods. It does not inspect local QGIS files, retrieve missing raster pixels, or independently verify planting suitability. Do not claim an existing SCVUFA partnership or that QGIS produced measured personal temperatures.'
}

HEAT_DETAILS = '''<div id="ecotrace-heat-definitions" class="mt-2 space-y-2 text-xs leading-5 text-slate-300">
<p><strong>Relative heat score (0-100):</strong> Preset city heat-reference values are blended by distance to the selected location. Higher scores indicate higher modeled relative heat; this is a comparison scale, not degrees or a health-risk measurement.</p>
<p><strong>Current air temperature:</strong> The latest available nearby NWS air-temperature observation. Station and observation time appear with the weather details. Missing weather is shown as unavailable; preset temperatures are not substituted.</p>
<p><strong>Separate measurements:</strong> The NWS box shows nearby weather. Dated satellite surface temperatures appear as separate candidate evidence where available. The QGIS surface-temperature layer does not replace these reference values.</p>
</div>'''
QGIS_DETAILS = '''<details id="ecotrace-qgis-preparation-v19" class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><summary class="cursor-pointer text-xs font-black text-emerald-200">QGIS preparation and candidate review</summary>
<div class="mt-2 space-y-2 text-xs leading-5 text-slate-300"><p>QGIS was used to overlay canopy on imagery, check alignment and pixel resolution, and clip canopy-cover and canopy-height layers to CA-17. EcoTrace samples the prepared canopy inputs when scoring route shade.</p>
<p>The 2022 canopy maps show mapped crowns; height rasters provide available vegetation heights. Missing pixels are unknown. Different capture years and missing height coverage remain sources of uncertainty. See Walking Routes below for sampling and height assumptions.</p>
<p>Dated surface-temperature imagery and mapped roads, sidewalks, buildings and transmission lines were also inspected for candidate screening. Such layers do not establish soil space, ownership, local utility clearance or permission. The separate 120-point trial was set aside and was not imported.</p>
<p><strong>Saved candidates:</strong> <span data-ecotrace-saved-count>Loading count...</span> candidate records. This is distinct from mapped existing trees and from the number currently displayed after filters.</p></div></details>'''
ABOUT = '''<div id="ecotrace-qgis-tree-v19" class="rounded-xl bg-white/5 p-3 ring-1 ring-white/10"><p class="text-xs leading-5 text-slate-300"><strong>How QGIS contributed:</strong> QGIS helped prepare and visually check local canopy, height and heat data. EcoTrace uses prepared spatial inputs for its route and candidate tools. Open Data &amp; Methods for the calculations and limitations.</p></div>'''
BUTTON = '''<a id="ecotrace-review-sites" href="/canopy-review" target="_blank" rel="noopener" class="mt-3 inline-flex w-full items-center justify-center rounded-xl bg-emerald-400/15 px-3 py-2 text-xs font-bold text-emerald-100 ring-1 ring-emerald-300/25">Review planting sites</a>'''

def current_weather_card(page):
    start = page.index('async function analyzeHeatPoint(')
    end = page.index('async function analyzePoint(', start)
    block = page[start:end]
    original = '${data.estimated_temperature_f}<span class="text-sm text-slate-400">°F</span>'
    if block.count(original) != 1:
        raise ValueError('Cannot identify the heat-card temperature value; no source files changed.')
    block = block.replace(original, '${liveTemp==null?\'Unavailable\':liveTemp}${liveTemp==null?\'\':\'<span class="text-sm text-slate-400">°F</span>\'}')
    block = block.replace('weather&&weather.available&&weather.temperature_f!=null', 'weather&&weather.available&&Number.isFinite(weather.temperature_f)')
    block = block.replace('window.latestEcoTraceAnalysis={...data,weather};',
        "window.latestEcoTraceAnalysis={...data,weather,current_air_temperature_f:(weather&&weather.available&&Number.isFinite(weather.temperature_f))?weather.temperature_f:null,displayed_temperature_source:'NWS nearby air observation; unavailable if missing'};")
    # Temperature is prominent once; the detail box retains feels-like, wind,
    # rain, station and timestamp.
    block = block.replace('<div><span class="text-slate-400">Observed temp.</span><br><strong>${weather.temperature_f??\'N/A\'}°F</strong></div>', '')
    block = block.replace('Live NWS observation unavailable; modeled heat remains available.', 'Weather unavailable.')
    block = re.sub(r'<p\b[^>]*>Tree recommendations appear only in the Tree Planting panel\.</p>', '', block)
    block = re.sub(r'<p\b[^>]*>The 0–100 heat score is a spatial EcoTrace exposure model;.*?</p>', '', block, flags=re.S)
    block = re.sub(r'<div\b[^>]*>\s*<p\b[^>]*>How shade is estimated</p>.*?</div>', '', block, flags=re.S)
    block = re.sub(r'<p\b[^>]*>\$\{escapeHtml\(data\.message\)\}</p>', '', block)
    return page[:start] + block + page[end:]


def audit_copy(page):
    page = re.sub(r'<p><strong>Coverage:</strong>\s*[\d,]+ planning candidates.*?</p>',
        '<p><strong>Coverage:</strong> <span data-ecotrace-saved-count>Loading count...</span> saved candidate records. Displayed results depend on location and filters.</p>', page, flags=re.S)
    page = page.replace('Apply spacing checks so nearby alternatives are not counted as separate trees.',
        'Spacing checks help screen nearby locations; candidate records are not a count of unique or planted trees.')
    page = page.replace('<strong>Screen each candidate:</strong> check the recorded image and map evidence',
        '<strong>Review candidate evidence:</strong> inspect available imagery and map evidence')
    page = page.replace('No screened site within 800 meters', 'No candidate returned nearby')
    page = page.replace('verified species images', 'species reference images')
    page = page.replace('curated 27-species recommendation set', 'curated species recommendation set')
    page = page.replace('curated 27-species list', 'curated species list')
    page = page.replace("EcoTrace’s curated 27-species list", "EcoTrace’s curated species list")
    page = page.replace("EcoTrace's 27 species", "EcoTrace's species")
    page = page.replace('curated_species_count:27', 'curated_species_count:null')
    page = page.replace('EcoTrace calculates a result from mapped inputs.',
        'EcoTrace calculates a result using data and stated assumptions.')
    page = page.replace('Four local rasters: two canopy-cover layers and two height layers. Open this section to load the installed model and valid canopy-pixel counts.',
        'Open this section to load the installed raster, mapped-record and valid canopy-pixel counts.')
    page = page.replace('Two 2022 canopy-cover rasters for the San Jos&eacute; and San Francisco&ndash;Oakland urban areas supply mapped crowns within the CA-17 clips. Two county height rasters supply Santa Clara 2020 vegetation-only heights and Alameda&ndash;Contra Costa heights from mixed survey years.',
        'Prepared 2022 canopy-cover layers for the San Jos&eacute; and San Francisco&ndash;Oakland urban areas supply mapped crowns where the CA-17 clips contain valid data. Available county height layers supply Santa Clara vegetation-only heights and Alameda&ndash;Contra Costa heights; survey years can differ.')
    page = page.replace('The separate 120-point trial was set aside and was not imported.', '')
    # Avoid presenting the heat endpoint's old absolute planting guarantee in
    # other result cards. The actual per-record evidence is shown separately.
    page = page.replace('${escapeHtml(data.message)}', "${escapeHtml(String(data.message||'').includes('they are never placed')?'Candidate locations use recorded map and imagery evidence.':data.message)}")
    return page


def transform(source):
    if 'ecotrace-methods-v19-script' in source: return source
    # Verify the calculation before describing it; never relabel a different model silently.
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'interpolate_heat']
    if len(functions) != 1 or not {'HEAT_ANCHORS', 'weighted_temp', 'weighted_heat'} <= {n.id for n in ast.walk(functions[0]) if isinstance(n, ast.Name)}:
        raise ValueError('Heat calculation differs from the reference model. No source files changed; send the current app source for matching labels.')
    pages = [n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'HTML_PAGE' for t in n.targets)]
    if len(pages) != 1 or not isinstance(pages[0], ast.Constant) or not isinstance(pages[0].value, str):
        raise ValueError('Cannot identify main app HTML safely; no source files changed.')
    page_node = pages[0]
    original_bytes = source.encode('utf-8')
    raw_lines = original_bytes.splitlines(keepends=True)
    page_start = sum(map(len, raw_lines[:page_node.lineno-1])) + page_node.col_offset
    page_end = sum(map(len, raw_lines[:page_node.end_lineno-1])) + page_node.end_col_offset
    source = page_node.value
    source = re.sub(r'<a\b[^>]*href=["\']/canopy-review["\'][^>]*>\s*Review planting sites\s*</a>', '', source)
    def insert_after_tag(pattern, value, label):
        nonlocal source
        found = list(re.finditer(pattern, source))
        if len(found) != 1: raise ValueError('Cannot identify '+label+' uniquely; no source files changed.')
        m = found[0]; source = source[:m.end()] + value + source[m.end():]
    insert_after_tag(r'<section\b[^>]*id="tree-panel"[^>]*>', BUTTON, 'Tree Planting panel')
    tree_summary = re.search(r'<summary\b[^>]*>[^<]*Tree Planting / Candidate Evidence</summary>', source)
    if tree_summary is None:
        raise ValueError('Cannot identify Tree Planting methods; no source files changed.')
    depth = 1
    for tag in re.finditer(r'</?details\b[^>]*>', source[tree_summary.end():]):
        depth += -1 if tag.group().startswith('</') else 1
        if depth == 0:
            position = tree_summary.end() + tag.start()
            qgis = QGIS_DETAILS.replace('</summary>', '</summary>' + ABOUT, 1)
            qgis = re.sub(r'<p>QGIS was used to overlay canopy on imagery,.*?</p>', '', qgis, count=1, flags=re.S)
            source = source[:position] + qgis + source[position:]
            break
    else:
        raise ValueError('Cannot find the end of Tree Planting methods; no source files changed.')
    insert_after_tag(r'<summary\b[^>]*>[^<]*Heat Map / Current Heat</summary>', HEAT_DETAILS, 'heat methods section')
    insert_after_tag(r'<summary\b[^>]*>[^<]*Gemini Assistant</summary>', '<p class="mt-2 text-xs leading-5 text-slate-300">Gemini receives the QGIS preparation methods, heat-label definitions and current saved candidate count alongside the app results. It explains these inputs; it does not inspect your local QGIS project or independently verify sites.</p>', 'Gemini methods section')
    score = re.compile(r'(<p\b[^>]*>)Modeled heat exposure(</p>)')
    temp = re.compile(r'(<p\b[^>]*>)Model baseline temp\.(</p>)')
    if not score.search(source) or not temp.search(source):
        raise ValueError('Cannot find the heat-card labels in this app version; no source files changed.')
    source = score.sub(lambda m:m[1]+'Relative heat score'+m[2], source)
    source = temp.sub(lambda m:m[1]+'Current air temperature'+m[2], source)
    source = current_weather_card(source)
    source = source.replace('Localized heat analysis', 'Local heat &amp; weather')
    source = source.replace('Walking heat &amp; shade', 'Route heat score &amp; sun/shade time')
    source = source.replace('Average modeled heat score:', 'Route-average relative heat score:')
    source = source.replace('Highest sampled heat score:', 'Highest sampled relative heat score:')
    source = source.replace('Model estimate · heat-map data and route shade', 'Reference heat model + estimated route shade')
    source = source.replace('Higher heat scores indicate hotter modeled surroundings. Sun/shade minutes estimate exposure at a constant walking pace.', 'Heat scores average the reference heat model along the route; they are not degrees or UTCI. Sun/shade minutes are separate estimates at a constant walking pace.')

    old = '<p><strong>EcoTrace calculation:</strong> sun position + nearby tree canopy + building geometry/heights → modeled shade/exposure score. NWS weather is shown as current context; it does not turn the score into a measured air-temperature map.</p>'
    source = source.replace(old, '<p><strong>Heat-map calculation:</strong> inverse-distance blending of preset city heat and temperature references. Route shade is calculated separately from sun position and mapped tree/building geometry. NWS weather is separate current context.</p>')
    # Replace a legacy hard-coded candidate count only inside About.
    a = source.index('id="guide-about"'); b = source.index('<section id="ewaste-panel"', a)
    about = re.sub(r'[\d,]+ mapped tree-planning candidates', '<span data-ecotrace-saved-count>saved</span> tree-planning candidate records', source[a:b])
    source = source[:a] + about + source[b:]
    source = audit_copy(source)
    script = Path(__file__).with_name('methods_update_ui.js').read_text()
    if source.count('</body>') != 1: raise ValueError('Unexpected app HTML structure; no source files changed.')
    source = source.replace('</body>', '<script id="ecotrace-methods-v19-script">'+script+'</script></body>')
    source = (original_bytes[:page_start] + repr(source).encode('utf-8') + original_bytes[page_end:]).decode('utf-8')
    compile(source, 'EcoTrace V19 methods and labels', 'exec')
    return source

@lru_cache(maxsize=1)
def _count(path, stamp, size):
    data=json.loads(Path(path).read_text())
    from canopy_editor import records
    return len(records(data))

def current_methods(root):
    p=Path(root)/'ecotrace_reviewed_tree_sites.json'
    try:
        stat=p.stat();count=_count(str(p),stat.st_mtime_ns,stat.st_size)
    except (OSError,ValueError,TypeError): count=None
    context={'version':VERSION,'saved_candidate_records':count,'methods':dict(METHODS)}
    try:
        from ecotrace_gis_runtime import status
        installed=status()
        context['loaded_rasters']=[{'name':x.get('name'),'kind':x.get('kind'),'height_unit':x.get('height_unit')} for x in installed.get('rasters',[])]
        context['mapped_tree_records']=installed.get('mapped_tree_count')
        context['mapped_building_records']=installed.get('mapped_building_count')
    except (ImportError,AttributeError,RuntimeError,OSError):
        context['loaded_rasters']=None
    return context

def install_runtime(app, root):
    if getattr(app,'_ecotrace_methods_v19',False): return
    root=Path(root)
    def methods_status(): return current_methods(root)
    app.add_api_route('/api/ecotrace-methods',methods_status,methods=['GET'])
    # The request schema already accepts guide_context. The server also supplies
    # the trusted definitions so older browser tabs cannot omit the clarification.
    import gemini_assistant_backend_v3 as backend
    original=getattr(backend._call_gemini,'_ecotrace_original',backend._call_gemini)
    def call(*,api_key,prompt):
        reference=json.dumps(current_methods(root),ensure_ascii=False,separators=(',',':'))
        prompt=('EcoTrace server-provided method definitions follow. Use them to interpret the numeric fields and correct older terminology in client context. Do not treat reference temperatures as current observations.\n'+reference+'\n\n'+prompt)
        return original(api_key=api_key,prompt=prompt)
    call._ecotrace_original=original
    backend._call_gemini=call
    app._ecotrace_methods_v19=True
