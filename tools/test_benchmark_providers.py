import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
import urllib.error
import benchmark_providers as b

T=datetime(2030,1,1,8,tzinfo=timezone.utc)
LOC=[{'position':{'lat':43.8,'lon':-79.3}},{'position':{'lat':43.6,'lon':-79.6}}]
class Tests(unittest.TestCase):
    def test_timezone_required(self):
        with self.assertRaises(RuntimeError): b.timestamp('2030-01-01T08:00')
        self.assertEqual(b.timestamp('2030-01-01T08:00Z'),T)
    def test_invalid_duration(self):
        for value in (None,-1,0,float('nan'),True,'oops'):
            with self.assertRaises(RuntimeError): b.seconds(value)
    def test_latest_uses_arrival_and_buffer(self):
        rows=[{'departure':T.isoformat(),'seconds':60},{'departure':(T+timedelta(minutes=1)).isoformat(),'seconds':180}]
        self.assertEqual(b.latest(rows,T+timedelta(minutes=3),1),rows[0])
    def test_wait_missed(self):
        with self.assertRaises(RuntimeError): b.wait_delay(T,T+timedelta(seconds=61))
        self.assertEqual(b.wait_delay(T,T-timedelta(seconds=3)),3)
    def test_comparison(self):
        live={'seconds':120,'requestStarted':T.isoformat(),'arrival':(T+timedelta(seconds=120)).isoformat()}
        result=b.comparison({'seconds':60},live,T,T+timedelta(seconds=90))
        self.assertEqual(result['changeMinutes'],1)
        self.assertEqual(result['deadlineMissMinutes'],.5)
    def test_google_depart_now_and_fallback(self):
        client=b.Client({'google':'secret','tomtom':'secret'})
        response={'routes':[{'duration':'61.2s','polyline':{'encodedPolyline':'abc'}}]}
        with patch.object(client,'request',return_value=response) as request:
            row=client.route('google',LOC)
            self.assertEqual(row['seconds'],61.2)
            self.assertNotIn('departureTime',request.call_args.args[2])
            self.assertTrue(request.call_args.args[2]['routeModifiers']['avoidTolls'])
        with patch.object(client,'request',return_value={'fallbackInfo':{'reason':'x'}}):
            with self.assertRaises(RuntimeError): client.route('google',LOC)
    def test_tomtom_arrival_and_geometry(self):
        client=b.Client({'google':'secret','tomtom':'secret'})
        response={'routes':[{'summary':{'departureTime':T.isoformat(),'travelTimeInSeconds':60},'legs':[{'points':[{'latitude':43.8,'longitude':-79.3}]}]}]}
        with patch.object(client,'request',return_value=response) as request:
            row=client.route('tomtom',LOC,arrival=T+timedelta(minutes=1))
            self.assertIn('arriveAt=',request.call_args.args[1])
            self.assertIn('computeTravelTimeFor=all',request.call_args.args[1])
            self.assertEqual(row['seconds'],60)
    def test_request_cap_no_call(self):
        client=b.Client({'tomtom':'secret'},{'tomtom':0})
        with patch('urllib.request.urlopen') as request:
            with self.assertRaises(RuntimeError): client.request('tomtom','https://example.com')
            request.assert_not_called()
    def test_error_secret_safe(self):
        client=b.Client({'tomtom':'secret'})
        with patch('urllib.request.urlopen',side_effect=urllib.error.URLError('https://example.com?key=secret')):
            with self.assertRaises(RuntimeError) as error: client.request('tomtom','https://example.com?key=secret')
        self.assertNotIn('secret',str(error.exception))
    def test_geocode_confidence_and_encoding(self):
        client=b.Client({'tomtom':'secret'})
        with patch.object(client,'request',return_value={'results':[{'position':{'lat':43,'lon':-79},'matchConfidence':{'score':.9},'address':{'freeformAddress':'Test'}}]}) as req:
            result=client.geocode('12 A/B Street')
            self.assertEqual(result['score'],.9)
            self.assertIn('12%20A%2FB%20Street',req.call_args.args[1])
if __name__=='__main__': unittest.main()

