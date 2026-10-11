"""Small generated rasters test geometry, units, masks, copying and integration."""
import ast
import json
import math
import sys
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from pyproj import Transformer
import ecotrace_gis_runtime as gis
from prepare_gis import prepare, FILES, copy_validate

LAT,LON = 37.4,-121.95


def make_raster(path, kind, crs='EPSG:3310', raw_height=30., blank=False):
    x,y=Transformer.from_crs('EPSG:4326',crs,always_xy=True).transform(LON,LAT)
    if crs=='EPSG:4326':
        # Native geographic raster like the user's reprojected Santa Clara file.
        rx,ry=1/(111320*math.cos(math.radians(LAT))),1/111320
    else:
        rx=ry=1.
    transform=from_origin(x-128*rx,y+128*ry,rx,ry)
    nodata=255 if kind=='cover' else -3.402823e38
    data=np.full((256,256),nodata,dtype='uint8' if kind=='cover' else 'float32')
    if not blank:
        if kind=='cover':
            data[116:124,124:132]=1 # canopy 4-12 m north of observer
        else:
            data[:]=raw_height
    with rasterio.open(path,'w',driver='GTiff',width=256,height=256,count=1,
                       dtype=data.dtype,crs=crs,transform=transform,nodata=nodata) as ds:
        ds.write(data,1)


@pytest.fixture
def environment(tmp_path):
    project=tmp_path/'app'; project.mkdir()
    (project/'ecotrace_repaired.py').write_text('# test placeholder')
    source=tmp_path/'qgis';source.mkdir()
    for i,(_,name,kind,_) in enumerate(FILES):
        crs='EPSG:4326' if i==2 else 'EPSG:6339' if i==3 else 'EPSG:3310'
        make_raster(source/name,kind,crs)
    manifest=prepare(project,source)
    return project,source,manifest


def test_real_preparation_copies_and_reuses_without_resampling(environment):
    project,source,manifest=environment
    assert len(manifest['rasters'])==4
    for entry in manifest['rasters']:
        with rasterio.open(entry['path']) as new, rasterio.open(source/entry['source_filename']) as old:
            assert new.compression.value=='DEFLATE'
            assert new.transform==old.transform
            assert np.array_equal(new.read(1),old.read(1))
    again=prepare(project,source)
    assert [x['path'] for x in again['rasters']]==[x['path'] for x in manifest['rasters']]


def test_feet_and_sun_direction(environment):
    provider=gis.Provider(environment[2])
    north=provider.shadow(LAT,LON,45,0)
    south=provider.shadow(LAT,LON,45,180)
    assert north['hit'] and not south['hit']
    assert north['height_m']==9.14
    assert not north['assumed_height']
    assert north['height_source'].startswith('Alameda')
    assert provider.shadow(LAT,LON,80,0)['hit'] is False


def test_unknown_units_use_labeled_assumption(environment):
    manifest=json.loads(json.dumps(environment[2]))
    for entry in manifest['rasters']:
        if entry['kind']=='height':entry['height_unit']='unknown'
    provider=gis.Provider(manifest)
    hit=provider.shadow(LAT,LON,45,0)
    assert hit['hit'] and hit['assumed_height'] and hit['height_m']==8
    counts=provider.status()['height_sample_counts']
    assert counts['total'] > 0
    assert counts['unconfirmed_units'] == counts['total']
    assert counts['missing_or_invalid'] == counts['measured'] == 0


def test_nodata_and_outside_extent_never_create_canopy(environment):
    provider=gis.Provider(environment[2])
    assert not provider.shadow(37.5,-121.8,45,0)['hit']
    assert not provider.shadow(LAT,LON,45,180)['hit']
    assert not provider.shadow(LAT,LON,-1,0)['hit']
    with pytest.raises(ValueError):provider.shadow(float('nan'),LON,45,0)


def test_missing_height_counts_are_distinct_from_unknown_units(environment):
    _,source,manifest=environment
    path=source/'missing_height.tif'
    make_raster(path,'height',blank=True)
    manifest=json.loads(json.dumps(manifest))
    manifest['rasters']=[x for x in manifest['rasters'] if x['kind']=='cover']+[
        dict(name='missing',kind='height',height_unit='feet',path=str(path))]
    provider=gis.Provider(manifest)
    result=provider.shadow(LAT,LON,45,0)
    counts=provider.status()['height_sample_counts']
    assert result['assumed_height']
    assert counts['total']>0 and counts['missing_or_invalid']==counts['total']
    assert counts['unconfirmed_units']==counts['measured']==0


def test_zero_height_not_mistaken_for_missing(environment):
    _,source,manifest=environment
    path=source/'zero.tif'
    make_raster(path,'height',raw_height=0)
    manifest=json.loads(json.dumps(manifest))
    manifest['rasters']=[x for x in manifest['rasters'] if x['kind']=='cover'] + [
        dict(name='zero',kind='height',height_unit='feet',path=str(path))]
    assert not gis.Provider(manifest).shadow(LAT,LON,45,0)['hit']


def test_blank_file_rejected(tmp_path):
    src=tmp_path/'empty.tif'
    make_raster(src,'cover',blank=True)
    with pytest.raises(ValueError,match='no positive valid data'):
        copy_validate(src,tmp_path/'out.tif','cover')
    assert not (tmp_path/'out.tif').exists()


def test_densification_preserves_endpoints_and_spacing():
    calls=[]
    distance=lambda a,b,c,d:111320*math.hypot(c-a,d-b)
    def segment(a,b,*_):
        calls.append((a,b));return (a,b)
    fn=gis.densified_builder(segment,distance)
    coords=[[0.,0.],[0.,.001],[.001,.001]]
    out=fn(coords,None,True)
    assert len(out)==16
    assert out[0][0]==coords[0] and out[-1][1]==coords[-1]
    assert all(distance(a[1],a[0],b[1],b[0])<=15 for a,b in out)


def test_source_augmentation_compiles_actual_app():
    source=Path(__file__).resolve().parent.parent/'ecotrace_import_source/ecotrace_final_input_polish_v6149.py'
    if not source.exists():pytest.skip('Current uploaded source is a development-only fixture.')
    result=gis.augment_source(source.read_text())
    ast.parse(result)
    assert 'GIS_SUMMARY + (' in result
    assert result.count("@app.get('/api/ecotrace-gis-status')")==1
    assert 'Local canopy data:</strong>' in result
    assert result.index('id="ecotrace-gis-methods"') < result.index('id="guide-about"')
    assert result.count('id="ecotrace-gis-methods"') == 1


def test_engine_fusion_keeps_building_model(environment,monkeypatch,tmp_path):
    fixture=Path(__file__).resolve().parent.parent/'ecotrace_import_source'
    if not fixture.exists():pytest.skip('Original canopy_engine is a development-only fixture.')
    monkeypatch.syspath_prepend(str(fixture))
    import canopy_engine
    original=canopy_engine.CanopyEngine
    monkeypatch.setattr(canopy_engine,'CanopyEngine',original)
    project,_,_=environment
    model=tmp_path/'model.json'
    model.write_text(json.dumps(dict(trees=[],buildings=[])))
    gis.activate(project)
    engine=canopy_engine.CanopyEngine(model)
    score,count=engine.tree_shadow_probability(LAT,LON,45,0)
    assert score==.65 and count==0
    # Max fusion must retain an existing stronger result, never sum both.
    monkeypatch.setattr(original,'tree_shadow_probability',lambda *a:(.9,7))
    score,count=engine.tree_shadow_probability(LAT,LON,45,0)
    assert score==.9 and count==7
    assert type(engine).building_shadow_probability is original.building_shadow_probability
    result=gis.probe(LAT,LON,45,0)
    assert result['existing_tree_score']==90 and result['combined_tree_score']==90


def test_legacy_about_notes_removed_without_removing_methods():
    source=Path(__file__).resolve().parent.parent/'ecotrace_import_source/ecotrace_final_input_polish_v6149.py'
    if not source.exists():pytest.skip('Uploaded app fixture not present.')
    raw=source.read_text()
    start=raw.index('>',raw.index('id="guide-about"'))+1
    for legacy in [
        '<p class="old">\n<strong>Walking comfort:</strong> old weather note</p><p><strong>Local canopy data:</strong> old canopy note</p>',
        '<section class="old"><p><strong>Local canopy data:</strong> old canopy note</p><details><summary>QGIS &amp; shade methods</summary>old details</details></section>'
    ]:
        result=gis.augment_source(raw[:start]+legacy+raw[start:])
        about=result[result.index('id="guide-about"'):result.index('<section id="ewaste-panel"')]
        assert 'Local canopy data:' not in about and 'Walking comfort:' not in about
        assert 'old details' not in about
        assert 'id="ecotrace-gis-methods"' in result
        ast.parse(result)
