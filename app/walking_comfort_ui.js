function walkingComfortHtml(data){
  const c=data.thermal_comfort;
  if(!c)return '';
  const f=(v)=>Number.isFinite(v)?Number(v).toFixed(1):'—';
  const title=c.mode==='modeled_exposure'?'Walking heat &amp; shade':'Walking thermal comfort · Experimental';
  const start=`<section class="mt-3 rounded-xl bg-orange-400/10 p-3 ring-1 ring-orange-300/25"><h3 class="text-sm font-black text-orange-100">${title}</h3>`;
  if(!c.available)return start+`<p class="mt-2 text-xs text-slate-300">${escapeHtml(c.reason||'Route exposure inputs are incomplete.')}</p></section>`;
  if(c.mode==='modeled_exposure'){
    const modelCard=(label,r)=>`<div class="mt-2 rounded-lg bg-black/15 p-2 text-xs text-slate-300">
      <p class="font-bold text-white">${label}</p>
      <p class="mt-1">Average modeled heat score: <strong>${f(r.mean_heat_score)}/100</strong></p>
      <p>Highest sampled heat score: ${f(r.peak_heat_score)}/100</p>
      <p class="mt-1">Estimated sun exposure: <strong>${f(r.sun_minutes)} min</strong></p>
      <p>Estimated time in shade: ${f(r.shade_minutes)} min</p>
      ${r.night_minutes>0?`<p>After sunset: ${f(r.night_minutes)} min</p>`:''}
      <p>Total walk: ${f(r.trip_minutes)} min</p>
    </div>`;
    return start+`<p class="mt-1 text-xs text-amber-200">Model estimate · heat-map data and route shade</p>`+
      modelCard('Shortest route',c.shortest)+((data.has_alternative_route ?? data.meaningful_shadier_route)?modelCard(escapeHtml(data.alternative_route_label||'Shade-oriented route'),c.shadiest):'')+
      `<p class="mt-2 text-[11px] text-slate-300">Higher heat scores indicate hotter modeled surroundings. Sun/shade minutes estimate exposure at a constant walking pace.</p>
      <details class="mt-2 text-[11px] text-slate-400"><summary>How this is calculated</summary><p>${escapeHtml(c.assumptions)}</p>
      ${!c.time_of_day_used?'<p>Ignore time uses the app’s neutral Sun scenario.</p>':''}</details></section>`;
  }
  const card=(label,r)=>`<div class="mt-2 rounded-lg bg-black/15 p-2 text-xs text-slate-300">
    <p class="font-bold text-white">${label}</p>
    <p class="mt-1">Average UTCI: <strong class="text-orange-100">${f(r.mean_utci_f)}°F / ${f(r.mean_utci_c)}°C</strong></p>
    <p>${escapeHtml(r.mean_category)}</p>
    <p class="mt-1">Hottest sampled segment: ${f(r.peak_utci_f)}°F UTCI</p>
    <p>Average radiant temperature (MRT): ${f(r.mean_mrt_f)}°F</p>
    <p>Time at UTCI ≥89.6°F (32°C): ${f(r.minutes_utci_ge_32c)} of ${f(r.trip_minutes)} min</p>
  </div>`;
  return start+`<p class="mt-1 text-xs font-bold text-amber-200">Sunny scenario using current nearby weather—not a forecast.</p>`+
    card('Shortest route',c.shortest)+((data.has_alternative_route ?? data.meaningful_shadier_route)?card(escapeHtml(data.alternative_route_label||'Shade-oriented route'),c.shadiest):'')+
    `<p class="mt-2 text-[11px] leading-5 text-slate-300">UTCI describes outdoor thermal conditions for a standard walker. MRT describes radiant exposure, not air or body temperature. Estimates do not change which route is selected.</p>
    <details class="mt-2 text-[11px] leading-5 text-slate-400"><summary class="cursor-pointer">Weather &amp; assumptions</summary>
    <p>${escapeHtml(c.station)} · ${escapeHtml(c.observation_time)}</p>
    <p>Air ${f(c.air_temperature_c)}°C · humidity ${f(c.relative_humidity_percent)}% · wind input ${f(c.wind_ms_used)} m/s${c.wind_floor_applied?' (raised to UTCI’s 0.5 m/s minimum)':''}.</p>
    <p>${escapeHtml(c.assumptions)}</p><p>Station wind approximates the 10 m UTCI input; local wind shelter is not modeled. Shading scores approximate body exposure. Sky view and surface temperatures are not measured.</p>
    </details></section>`;
}
