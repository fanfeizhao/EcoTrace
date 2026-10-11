(() => {
  async function refreshMethods() {
    try {
      const response = await fetch('/api/ecotrace-methods', {cache:'no-store'});
      if (!response.ok) throw Error('Methods unavailable');
      const data = await response.json();
      const guide = window.latestGuideContext || {};
      guide.qgis_methods = data.methods;
      guide.saved_candidate_records = data.saved_candidate_records;
      guide.loaded_gis_rasters = data.loaded_rasters;
      guide.exact_sources = {...guide.exact_sources,
        heat:['Preset city heat and temperature references (inverse-distance model)','NWS nearby observation and hourly forecast (separate weather context)']};
      for (const key of ['walking','biking','tree_planting']) {
        guide.exact_sources[key] = [...new Set([...(guide.exact_sources[key] || []), 'QGIS-prepared 2022 canopy cover and available county canopy heights'])];
      }
      guide.core_message = 'EcoTrace separates reference heat modeling, nearby weather, dated surface heat, geometric route shade and candidate review. QGIS prepares and checks the spatial inputs; Gemini explains the supplied app results and methods.';
      window.latestGuideContext = guide;
      document.querySelectorAll('[data-ecotrace-saved-count]').forEach(el => {
        el.textContent = Number.isInteger(data.saved_candidate_records) ? data.saved_candidate_records.toLocaleString() : 'count unavailable';
      });
    } catch (_) {
      document.querySelectorAll('[data-ecotrace-saved-count]').forEach(el => el.textContent='count unavailable');
    }
  }
  refreshMethods();
  window.addEventListener('focus', refreshMethods);
})();
