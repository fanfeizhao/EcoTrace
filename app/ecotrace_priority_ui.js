function treePriorityHtml(site){
  if(!site.planning_priority)return '';
  const e=escapeHtml;
  const p=site.planning_priority;
  const color=p.startsWith('Priority 1')?'#fca5a5':p.startsWith('Priority 2')?'#fcd34d':p==='Data incomplete'?'#cbd5e1':'#86efac';
  const poverty=site.priority_poverty_percent==null?'Unavailable':Number(site.priority_poverty_percent).toFixed(1)+'%';
  const heat=site.priority_heat_c==null?'Unavailable':Number(site.priority_heat_c).toFixed(1)+'°C / '+(Number(site.priority_heat_c)*1.8+32).toFixed(1)+'°F';
  return `<details class="mt-2 text-xs" style="margin-top:8px;font-size:12px;line-height:1.5"><summary style="cursor:pointer;font-weight:700">Planning priority &amp; why</summary>
  <div style="margin:10px 0;padding:10px;border-left:3px solid ${color};background:rgba(255,255,255,.05);font-size:12px;line-height:1.5">
    <strong style="color:${color}">${e(p)}</strong><span> · experimental planning priority</span>
    <p>Historical surface heat: ${e(heat)}</p>
    <p>Tract poverty: ${e(poverty)} · Median household income: ${e(site.priority_income_text||'Unavailable')}</p>
    <p><strong>Why this priority?</strong> ${e(site.priority_reason||'')}</p>
    <p>${e(site.priority_heat_status||'')} · ${e(site.priority_poverty_status||'')}</p>
    <p>ACS 2020–2024, tract ${e(site.priority_tract||'unavailable')}. Neighborhood statistics, not this household.</p>
    ${site.priority_poverty_moe_pp!=null?`<p>Poverty margin of error: ±${Number(site.priority_poverty_moe_pp).toFixed(1)} percentage points.</p>`:''}
    <p>${site.priority_estimate_sensitive?'Estimate-sensitive: the Census poverty margin of error crosses the priority threshold.':'Priority is based on historical estimates, not current conditions.'}</p>
    <p>Heat uses at least two cloud-screened satellite observations covering this coordinate, with about 100 m thermal detail. It is not a tree-sized or current air-temperature reading.</p>
    <p>Income is descriptive; this label uses historical heat and tract poverty. Permission and site checks still required.</p>
    <a href="/tree-priority" target="_blank" rel="noopener" style="text-decoration:underline">All-site statistics and methods ↗</a>
  </div></details>`;
}
function treeEvidenceHtml(site){return treeEvidenceHtmlBase(site)+treePriorityHtml(site);}
