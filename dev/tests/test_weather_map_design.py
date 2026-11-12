"""Dark layout, band preservation, pagination and embedded-font regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_route_weather_map import fixture, mod, Map, NOW, decode_png
from _harness import install_verb_guard, restore_verb_guard


def setUpModule():
    global originals
    originals = install_verb_guard(mod.requests)


def tearDownModule():
    restore_verb_guard(mod.requests, originals)


def many_bands(row, count=10):
    """Presentation fixture only: do not alter producer sampling limits/density."""
    row=copy.deepcopy(row)
    sections=row['route_weather']['sections']
    assert len(sections)>=count
    poly=row['terrain_summary']['polyline']
    points=[]
    for i in range(count):
        group=sections[i*len(sections)//count:(i+1)*len(sections)//count]
        km=(group[0]['start_km']+group[-1]['end_km'])/2
        p=min(poly,key=lambda p:abs(p[0]-km))
        points.append({'point_id':i,'km':km,'lat':p[1],'lon':p[2],'altitude':p[3]})
        for s in group:
            s['point_id']=i
    row['route_weather']['sampling']['points']=points
    row['route_weather']['bands']=mod.RouteWeather.forecast_bands(row['route_weather'])
    return row


class MapDesignTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sync=mod.IntervalsSync.__new__(mod.IntervalsSync)
        self.sync.data_dir=Path(tmp.name)
        self.sync.athlete_id='synthetic-design'
        self.sync.debug=False
        self.sync.route_weather_enabled=True
        self.row,self.evt=fixture(self.sync)
        self.sync._routes_data={'events':[self.row]}

    def generate(self,row=None):
        self.sync._routes_data={'events':[row or self.row]}
        with mock.patch.object(Map,'png',return_value=b'synthetic-png'):
            Map.generate(self.sync,[self.evt],{'timezone':'UTC'},NOW)

    def test_short_name_full_size_and_long_name_wrap_and_ellipsis(self):
        self.assertEqual(Map.name_layout('Coastal loop'),(['Coastal loop'],44))
        long='Sønderborg – Wernigerode scenic forest and hilltop endurance loop'
        lines,size=Map.name_layout(long)
        self.assertEqual(size,40)
        self.assertEqual(len(lines),2)
        self.assertTrue(lines[-1].endswith('…'))
        for line in lines:
            self.assertLessEqual(mod.RouteMapFont.width(line,size,True),464)
        self.assertEqual(long,'Sønderborg – Wernigerode scenic forest and hilltop endurance loop')

    def test_name_shrinks_before_wrapping_and_long_token_is_safe(self):
        name=next('W'*n for n in range(1,30) if mod.RouteMapFont.width('W'*n,44,True)>464
                  and mod.RouteMapFont.width('W'*n,40,True)<=464)
        lines,size=Map.name_layout(name)
        self.assertEqual(lines,[name])
        self.assertTrue(40<=size<44)
        lines,size=Map.name_layout('W'*100)
        self.assertEqual(len(lines),2)
        self.assertTrue(lines[1].endswith('…'))

    def test_full_name_preserved_in_brief_and_overview(self):
        long='Ærø ' * 40
        self.evt['name']=long
        with mock.patch.dict(fixture.__globals__,{'event':lambda eid,**values: dict(self.evt,**values)}):
            self.row,_=fixture(self.sync)
        self.assertEqual(self.row['route_weather']['name'],long)
        self.sync._routes_data={'events':[self.row]}
        self.sync._overview_events=[self.evt]
        self.sync._overview_athlete={'timezone':'UTC'}
        overview=mod.DataOverview.build(self.sync,{},now=NOW)
        self.assertEqual(overview['rides'][0]['name'],long)
        before=copy.deepcopy(self.row['route_weather'])
        Map.scene(self.row)
        self.assertEqual(self.row['route_weather'],before)

    def test_wrap_grows_header_and_preserves_fixed_start_card(self):
        short=Map.scene(self.row)
        self.row['route_weather']['name']='Wernigerode and Sønderborg forest endurance loop across the hills'
        long=Map.scene(self.row)
        self.assertEqual(long[0][2]-short[0][2],48)
        for scene in (short,long):
            self.assertIn(('rect',540,40,500,112,Map.CARD,16),scene)

    def test_ten_bands_paginate_without_merging_or_sampling_change(self):
        row=many_bands(self.row)
        before=copy.deepcopy(row)
        scenes=Map.scenes(row)
        self.assertEqual(len(scenes),2)
        self.assertEqual(row,before)
        self.assertEqual(mod.RouteWeather.MAX_POINTS,6)
        rows=[[i for i in scene if i[0]=='rect' and i[3:5]==(1000,98)] for scene in scenes]
        self.assertEqual([len(r) for r in rows],[8,2])
        text='\n'.join(i[3] for i in scenes[1] if i[0]=='text')
        self.assertIn('Sections continued · 2/2',text)
        self.assertIn('I',text); self.assertIn('J',text)
        self.assertNotIn('WIND ALONG THE ROUTE',text)
        self.assertFalse(any(i[0]=='polygon' for i in scenes[1]))

    def test_eight_bands_stay_on_one_page(self):
        self.assertEqual(len(Map.scenes(many_bands(self.row,8))),1)

    def test_dominant_is_distance_weighted_not_first_sample_or_count(self):
        def segment(start,end,direction):
            return {'start_km':start,'end_km':end,'bearing_deg':0,
                    'weather':{'map_sample':{'wind_speed_mps':5,'wind_from_deg':direction}}}
        segments=[segment(0,.1,0),segment(.1,.2,0),segment(.2,1.2,90)]
        self.assertEqual(Map.sample_kind(segments[0]),'head')
        self.assertEqual(Map.dominant(segments),'cross')
        self.assertEqual(Map.dominant(list(reversed(segments))),'cross')
        self.assertEqual(Map.direction(segments),'E')

    def test_dominant_label_controls_bar_and_text(self):
        row=copy.deepcopy(self.row)
        group=[s for s in row['route_weather']['sections'] if s['point_id']==0]
        for s in group:
            s['weather']['map_sample'].update(wind_speed_mps=5,wind_from_deg=(s['bearing_deg']+90)%360)
        row['route_weather']['bands']=mod.RouteWeather.forecast_bands(row['route_weather'])
        self.assertEqual(Map.bands(row['route_weather'])[0]['wind_class'],'cross')
        scene=Map.scene(row)
        self.assertIn(('rect',40,1222,10,98,Map.CROSS,5),scene)
        self.assertTrue(any(i[0]=='text' and i[3]=='Crosswind' and i[2]==1264 for i in scene))

    def test_ordered_page_metadata_and_publication_list(self):
        row=many_bands(self.row)
        self.generate(row)
        descriptor=row['route_weather_map']
        self.assertEqual(descriptor['page_count'],2)
        self.assertEqual([p['page'] for p in descriptor['pages']],[1,2])
        self.assertTrue(descriptor['pages'][1]['path'].endswith('-p02.png'))
        self.sync._overview_events=[self.evt]
        self.sync._overview_athlete={'timezone':'UTC'}
        overview=mod.DataOverview.build(self.sync,{},now=NOW)
        image=overview['rides'][0]['map']
        self.assertTrue(image['present'])
        self.assertEqual([p['path'] for p in image['pages']],[p['path'] for p in descriptor['pages']])
        self.assertTrue(all(p['path'] in overview['managed_files'] for p in descriptor['pages']))
        self.assertEqual(overview['folders'][0]['managed_count'],2)

    def test_page_cleanup_on_shrink_off_and_removal_preserves_unrelated(self):
        row=many_bands(self.row)
        self.generate(row)
        pages=[self.sync.data_dir/p['path'] for p in row['route_weather_map']['pages']]
        unrelated=pages[0].parent/'personal-p02.png'
        unrelated.write_bytes(b'keep')
        self.generate(self.row)
        self.assertTrue(pages[0].exists()); self.assertFalse(pages[1].exists())
        self.generate(row)
        self.sync.route_weather_enabled=False
        self.generate(row)
        self.assertFalse(any(p.exists() for p in pages))
        self.assertEqual(unrelated.read_bytes(),b'keep')

    def test_missing_page_is_explicit_and_bad_page_paths_rejected(self):
        row=many_bands(self.row)
        self.generate(row)
        descriptor=row['route_weather_map']
        (self.sync.data_dir/descriptor['pages'][1]['path']).unlink()
        self.sync._overview_events=[self.evt]
        self.sync._overview_athlete={'timezone':'UTC'}
        image=mod.DataOverview.build(self.sync,{},now=NOW)['rides'][0]['map']
        self.assertFalse(image['present'])
        self.assertEqual(image['reason'],'page_missing')
        self.assertEqual([p['present'] for p in image['pages']],[True,False])
        for path in ('../bad.png','maps/weather/personal.png',descriptor['path']):
            bad=copy.deepcopy(descriptor); bad['pages'][1]['path']=path
            self.assertEqual(mod.DataOverview.map_pages(bad),[])

    def test_second_page_symlink_does_not_write_first_or_touch_target(self):
        row=many_bands(self.row)
        directory=self.sync.data_dir/Map.DIRECTORY
        directory.mkdir(parents=True)
        stem='weather-'+mod.RouteWeather.digest(str(row['event_id']))[:32]
        target=self.sync.data_dir/'keep.txt'; target.write_text('keep')
        (directory/(stem+'-p02.png')).symlink_to(target)
        self.generate(row)
        self.assertNotIn('route_weather_map',row)
        self.assertFalse((directory/(stem+'.png')).exists())
        self.assertEqual(target.read_text(),'keep')
        self.assertTrue((directory/(stem+'-p02.png')).is_symlink())

    def test_page_limits_fail_closed_before_any_write(self):
        row=many_bands(self.row)
        with mock.patch.object(Map,'MAX_EVENT_BYTES',1):
            self.generate(row)
        self.assertNotIn('route_weather_map',row)
        self.assertFalse((self.sync.data_dir/Map.DIRECTORY).exists())
        bad=copy.deepcopy(row)
        bad['route_weather']['sampling']['points']*=4
        with self.assertRaises(ValueError):
            Map.scenes(bad)

    def test_continuation_png_is_deterministic_and_dark(self):
        scene=Map.scenes(many_bands(self.row))[1]
        first=Map.png(scene)
        self.assertEqual(first,Map.png(scene))
        width,height,pixels=decode_png(first)
        self.assertEqual((width,height),(1080,scene[0][2]))
        self.assertEqual(pixels[1:4],bytes(Map.BACKGROUND))
        self.assertLess(len(first),Map.MAX_BYTES)

    def test_native_rain_period_is_not_relabelled_as_hourly(self):
        sections=copy.deepcopy(self.row['route_weather']['sections'])
        for section in sections:
            for period in section['weather']['precipitation_intervals']:
                period['period_hours']=6
        label, probability, period=Map.rain(sections)
        self.assertIn('mm/6h',label)
        self.assertNotIn('mm/h',label)
        self.assertGreaterEqual(probability,30)

    def test_cross_date_arrival_range_fits_fixed_card(self):
        row=copy.deepcopy(self.row)
        row['route_weather']['eta']['duration_range_s']=[90000,180000]
        scene=Map.scene(row)
        finish=[item for item in scene if item[0]=='text' and item[1]==822 and item[2] in (105,136)]
        self.assertEqual(len(finish),2)
        self.assertIn('20/10',finish[0][3])
        self.assertIn('21/10',finish[1][3])
        self.assertIn(('rect',540,40,500,112,Map.CARD,16),scene)

    def test_publication_validates_and_removes_all_ordered_pages(self):
        from test_data_overview import workflow_rules, tiny_png
        row=many_bands(self.row)
        self.sync._routes_data={'events':[row]}
        with mock.patch.object(Map,'png',return_value=tiny_png()):
            Map.generate(self.sync,[self.evt],{'timezone':'UTC'},NOW)
        self.sync._overview_events=[self.evt]
        self.sync._overview_athlete={'timezone':'UTC'}
        overview=mod.DataOverview.build(self.sync,{},now=NOW)
        root=self.sync.data_dir
        previous={'data_overview':overview}
        (root/'latest.json').write_text(json.dumps(previous))
        rules=workflow_rules()
        pages=[p['path'] for p in row['route_weather_map']['pages']]
        self.assertTrue(set(pages)<=set(rules['plan'](root)['saved']))
        self.sync.route_weather_enabled=False
        self.generate(row)
        current={'data_overview':mod.DataOverview.build(self.sync,{},now=NOW)}
        (root/'latest.json').write_text(json.dumps(current))
        self.assertEqual(set(rules['plan'](root,previous)['removed']),set(pages))

    def test_four_pages_per_ride_and_six_ride_retention_bound(self):
        from test_route_snapshots import hundred_km_row
        pairs=[]
        for eid in range(8):
            row,evt=fixture(self.sync,terrain=hundred_km_row(self.row)['terrain_summary'],eid=eid)
            pairs.append((many_bands(row,32),evt))
        self.sync._routes_data={'events':[r for r,e in pairs]}
        with mock.patch.object(Map,'png',return_value=b'png'):
            Map.generate(self.sync,[e for r,e in pairs],{'timezone':'UTC'},NOW)
        retained=[r for r,e in pairs if 'route_weather_map' in r]
        self.assertEqual(len(retained),6)
        self.assertEqual([r['route_weather_map']['page_count'] for r in retained],[4]*6)
        self.assertEqual(len(list((self.sync.data_dir/Map.DIRECTORY).glob('*.png'))),24)
        for r in retained:
            scenes=Map.scenes(r)
            self.assertEqual([sum(i[0]=='rect' and i[3:5]==(1000,98) for i in s) for s in scenes],[8]*4)


    def test_start_label_is_near_ring_and_omitted_when_blocked(self):
        point=(500,500)
        ring=[('circle',500,500,19,Map.WHITE)]
        self.assertEqual(Map.start_label_position(point,'Start / finish',ring),(533,507))
        # A line crossing the first choice makes another adjacent choice necessary.
        obstruction=ring+[('line',(570,400),(570,600),Map.CASING,22,'solid')]
        self.assertNotEqual(Map.start_label_position(point,'Start / finish',obstruction),(533,507))
        self.assertIsNotNone(Map.start_label_position(point,'Start / finish',obstruction))
        blocked=ring+[('rect',40,294,1000,572,Map.CARD,0)]
        self.assertIsNone(Map.start_label_position(point,'Start / finish',blocked))

    def test_forecast_and_outlook_never_use_orphan_start_text(self):
        import math
        for outlook in (False,True):
            row,_=fixture(self.sync,outlook=outlook)
            scene=Map.scene(row)
            rings=[(op[1],op[2]) for op in scene if op[0]=='circle' and op[3]==19]
            for op in scene:
                if op[0]=='text' and op[3] in ('Start','Finish','Start / finish'):
                    self.assertNotEqual(op[1:3],(56,303))
                    width=mod.RouteMapFont.width(op[3],op[4],op[6])
                    # Distance from ring center to the actual label bounding box.
                    distance=min(math.dist(p,(max(op[1],min(p[0],op[1]+width)),
                                              max(op[2]-20,min(p[1],op[2]+6)))) for p in rings)
                    self.assertLessEqual(distance,45)
        with mock.patch.object(Map,'start_label_position',return_value=None):
            scene=Map.scene(self.row)
        self.assertFalse(any(op[0]=='text' and op[3] in ('Start','Finish','Start / finish') for op in scene))
        self.assertEqual(sum(op[0]=='circle' and op[3]==19 for op in scene),2)

    def test_png_timezone_abbreviation_and_offset_fallback_preserve_json(self):
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        self.assertEqual(Map.zone_label(datetime(2026,10,20,tzinfo=ZoneInfo('Europe/Copenhagen'))),'CEST')
        self.assertEqual(Map.zone_label(datetime(2026,12,20,tzinfo=ZoneInfo('Europe/Copenhagen'))),'CET')
        self.assertEqual(Map.zone_label(datetime(2026,10,20,tzinfo=ZoneInfo('Etc/GMT-2'))),'UTC+02:00')
        unnamed=mock.Mock()
        unnamed.tzname.return_value=None
        unnamed.utcoffset.return_value=timedelta(hours=-3,minutes=-30)
        self.assertEqual(Map.zone_label(unnamed),'UTC-03:30')
        row,evt=fixture(self.sync,athlete={'timezone':'Europe/Copenhagen'})
        before=copy.deepcopy(row['route_weather'])
        labels=[op[3] for op in Map.scene(row) if op[0]=='text']
        self.assertTrue(any('CEST' in label for label in labels))
        self.assertFalse(any('CEST+0200' in label for label in labels))
        self.assertEqual(row['route_weather'],before)
        self.assertIn('CEST+0200',before['summary']['display']['departure'])
        self.sync._routes_data={'events':[row]}
        self.sync._overview_events=[evt]
        self.sync._overview_athlete={'timezone':'Europe/Copenhagen'}
        self.assertIn('CEST+0200',mod.DataOverview.build(self.sync,{},now=NOW)['rides'][0]['departure_display'])

    def test_display_ranges_collapse_after_rounding_without_changing_shared_label(self):
        for unit in ('m/s','mph','C','F'):
            # Display conversion rounds the distinct canonical endpoints to one decimal.
            value={'value':[round(4.01,1),round(4.04,1)],'unit':unit}
            suffix='°'+unit if unit in ('C','F') else unit
            self.assertEqual(Map.display_label(value),'4.0 '+suffix)
            self.assertEqual(Map.label(value),'4-4 '+unit)
        self.assertEqual(Map.display_label({'value':[4,4.1],'unit':'m/s'}),'4.0–4.1 m/s')
        self.assertEqual(Map.display_label({'value':[-2,-1],'unit':'C'}),'-2.0–-1.0 °C')
        self.assertEqual(Map.display_span(['1h','1h']),'1h')
        row=copy.deepcopy(self.row)
        for segment in row['route_weather']['sections']:
            segment['weather']['display']['wind_speed_range_mps']['value']=[4,4]
            segment['weather']['display']['temperature_range_c']['value']=[9,9]
        row['route_weather']['bands']=mod.RouteWeather.forecast_bands(row['route_weather'])
        labels=[op[3] for op in Map.scene(row) if op[0]=='text']
        self.assertIn('9.0 °C',labels)
        self.assertTrue(any('4.0 m/s from' in label for label in labels))
        self.assertFalse(any('4–4' in label or '9–9' in label for label in labels))

    def test_outlook_uses_short_local_window_degree_and_readable_wind(self):
        row,_=fixture(self.sync,outlook=True,athlete={'timezone':'Europe/Copenhagen'})
        brief=row['route_weather']
        brief['outlook']['window_utc']=['2026-10-23T06:00:00Z','2026-10-24T00:00:00Z']
        brief['summary']['display']['temperature_range']={'value':[9,16],'unit':'C'}
        for direction,message in (('VARIABLE','Wind variable'),('UNKNOWN','Wind unknown'),
                                   ('MOSTLY W','Wind from W'),('MOSTLY W-NW','Wind from W/NW')):
            brief['outlook']['display_wind_from']=direction
            before=copy.deepcopy(brief)
            scene=Map.scene(row)
            labels=[op[3] for op in scene if op[0]=='text']
            self.assertIn('Fri 23 Oct 08:00 – Sat 24 Oct 02:00',labels)
            self.assertIn('9.0–16.0 °C · '+message,labels)
            self.assertEqual(brief,before)
            window=next(op for op in scene if op[0]=='text' and op[3]=='Fri 23 Oct 08:00 – Sat 24 Oct 02:00')
            self.assertEqual(window[4:7],(23,Map.SECONDARY,False))
            detail=next(op for op in scene if op[0]=='text' and op[3].startswith('9.0–16.0'))
            self.assertEqual(detail[4:7],(26,Map.WHITE,False))

    def test_outlook_dst_window_keeps_distinct_local_zone_labels(self):
        row,_=fixture(self.sync,outlook=True,athlete={'timezone':'Europe/Copenhagen'})
        row['route_weather']['outlook']['window_utc']=['2026-10-25T00:00:00Z','2026-10-25T01:00:00Z']
        labels=[op[3] for op in Map.scene(row) if op[0]=='text']
        self.assertIn('Sun 25 Oct 02:00 CEST – Sun 25 Oct 02:00 CET',labels)

    def test_font_coverage_fallback_and_synthetic_bold(self):
        font=mod.RouteMapFont
        for char in '–·°↑↓←→ÆØÅæøåÄÖÜäöüßŁł…':
            self.assertIn(char,font.glyphs())
        self.assertEqual(font.clean('A\x00雪'),'A??')
        regular,bold=font.glyph('A',40),font.glyph('A',40,True)
        self.assertGreater(sum(bold[-1]),sum(regular[-1]))
        self.assertIn('SIL OPEN FONT LICENSE Version 1.1',font.FONT_LICENSE)
        self.assertFalse(hasattr(Map,'FONT'))


if __name__=='__main__':
    unittest.main()
