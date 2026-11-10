import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).with_name('home-monitoring-loki-bridge.py'))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def vector(rows):
    return {'status':'success','data':{'resultType':'vector','result':[
        {'metric':{'namespace':namespace,'app':app},'value':[123,str(count)]}
        for namespace,app,count in rows]}}


class Tests(unittest.TestCase):
    def test_checkpoint_deduplicates_same_bucket(self):
        loader=lambda query,end: {('network','envoy'):2}
        state,events=b.step({},1230,loader)
        self.assertEqual(state['last_bucket'],1200)
        self.assertEqual(len(events),2)
        with tempfile.TemporaryDirectory() as d:
            b.persist(Path(d),state,events,1230)
            restored=json.loads((Path(d)/'state.json').read_text())
        _,repeated=b.step(restored,1240,loader)
        self.assertEqual(repeated,[])

    def test_failure_does_not_advance_or_fabricate_quiet(self):
        def failure(*args):raise ValueError('broken')
        original={'last_bucket':900,'healthy':True}
        state,events=b.step(original,1230,failure)
        self.assertEqual(state['last_bucket'],900)
        self.assertFalse(state['healthy'])
        self.assertFalse(any(e['event_type']=='error_summary' for e in events))
        state,events=b.step(state,1290,lambda q,e:{})
        self.assertEqual(state['last_bucket'],1200)
        self.assertEqual(events[-1]['status'],'recovered')
        self.assertEqual(events[0]['status'],'no_logs_observed')

    def test_invalid_or_truncated_vectors_rejected(self):
        for bad in [vector([])|{'warnings':['partial']},
                    {'status':'success','data':{'resultType':'streams','result':[]}},
                    vector([('network','envoy',float('nan'))]),
                    vector([('network','envoy',-1)]),
                    vector([('default','neatplan',1)]),
                    vector([('network','envoy',1),('network','envoy',1)])]:
            with self.assertRaises((ValueError,TypeError)):b.parse(bad)
        with patch.object(b,'MAX_SERIES',1):
            with self.assertRaises(ValueError):b.parse(vector([('network','a',1),('network','b',1)]))

    def test_request_cap_detects_one_extra_series(self):
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):return json.dumps(vector([('network',str(n),1) for n in range(b.MAX_SERIES+1)])).encode()
        with patch.object(b.urllib.request,'urlopen',return_value=Response()) as mocked:
            with self.assertRaises(ValueError):b.fetch('http://localhost','query',1200)
            self.assertIn('limit=96',mocked.call_args.args[0].full_url)

    def test_gap_is_explicit_and_success_dependent(self):
        state,events=b.step({'last_bucket':300},30000,lambda q,e:{})
        self.assertEqual(events[-1]['event_type'],'collection_gap')
        self.assertGreater(events[-1]['skipped_buckets'],0)
        self.assertEqual(state['last_bucket'],29700)

    def test_no_secret_log_fields_exported(self):
        rows=b.parse(vector([('network','envoy',3)]))
        events=b.summarize(rows,rows,1200)
        self.assertEqual(events[0]['matched_lines'],3)
        self.assertNotIn('message',events[0])
        self.assertNotIn('log',events[0])
        self.assertLessEqual(len(b.summarize({('network',str(i)):1 for i in range(95)},
                                           {('network',str(i)):1 for i in range(95)},1200))+2,100)

    def test_inconsistent_queries_preserve_cursor(self):
        calls=iter([{('network','envoy'):5},{('network','envoy'):4}])
        state,events=b.step({'last_bucket':900},1230,lambda q,e:next(calls))
        self.assertEqual(state['last_bucket'],900)
        self.assertEqual(events[0]['status'],'unavailable')


if __name__=='__main__':unittest.main()
