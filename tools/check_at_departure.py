"""Wait for saved departure, fetch one live ETA, and open Google Maps.
Run now and keep this terminal and computer awake. No automatic retries.
"""
import argparse
import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent

def comparison(saved, live_seconds, requested_at):
    predicted = float(saved['recommendation']['seconds'])
    scheduled = datetime.fromisoformat(saved['recommendation']['departure'])
    deadline = datetime.fromisoformat(saved['deadline'])
    return {'earlierDurationSeconds':predicted, 'liveDurationSeconds':live_seconds,
            'durationChangeSeconds':live_seconds-predicted,
            'liveEstimatedArrival':(requested_at+timedelta(seconds=live_seconds)).isoformat(),
            'deadlineDifferenceSeconds':(requested_at+timedelta(seconds=live_seconds)-deadline).total_seconds(),
            'scheduleDelaySeconds':(requested_at-scheduled).total_seconds()}

def load_inputs(path):
    saved = json.loads(path.read_text(encoding='utf-8'))
    if not saved.get('recommendation'):
        raise RuntimeError('Saved forecast has no departure recommendation.')
    target = datetime.fromisoformat(saved['recommendation']['departure'])
    if target.utcoffset() is None:
        raise RuntimeError('Saved departure must include a timezone offset.')
    trip = json.loads((ROOT/'.local'/'trip.json').read_text(encoding='utf-8'))
    if trip['deadline'] != saved['deadline'] or trip.get('avoidTolls',True) != saved.get('avoidTolls',True):
        raise RuntimeError('Trip settings no longer match the saved forecast.')
    text = (ROOT/'.env.local').read_text(encoding='utf-8-sig')
    match = re.search(r'^GOOGLE_MAPS_API_KEY\s*=\s*(.*?)\s*$',text,re.M)
    key = match.group(1).strip().strip(chr(34)+chr(39)) if match else ''
    if not key or key == 'your_key_here':
        raise RuntimeError('Local API key is missing.')
    return saved, target, trip, key

def fetch_live(trip, key):
    # Omitting departureTime requests departure now, not a timestamp already past.
    body = {'origin':{'address':trip['origin']}, 'destination':{'address':trip['destination']},
            'travelMode':'DRIVE', 'routingPreference':'TRAFFIC_AWARE_OPTIMAL', 'trafficModel':'BEST_GUESS',
            'computeAlternativeRoutes':False, 'routeModifiers':{'avoidTolls':trip.get('avoidTolls',True),'avoidHighways':False,'avoidFerries':True},'units':'METRIC'}
    request = urllib.request.Request('https://routes.googleapis.com/directions/v2:computeRoutes',
        data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json',
        'X-Goog-Api-Key':key,'X-Goog-FieldMask':'routes.duration,routes.distanceMeters,routes.description,routes.legs.startLocation,routes.legs.endLocation,fallbackInfo'})
    began = time.perf_counter()
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('Google returned HTTP '+str(error.code)+'. No automatic retry.') from None
    except (urllib.error.URLError,TimeoutError):
        raise RuntimeError('Connection failed or timed out. No automatic retry.') from None
    if data.get('fallbackInfo'):
        raise RuntimeError('Fallback routing returned; not a full traffic-aware comparison.')
    routes = data.get('routes',[])
    if not routes:
        raise RuntimeError('No route returned.')
    route = routes[0]
    if not re.fullmatch(r'\d+(?:\.\d+)?s',route.get('duration','')):
        raise RuntimeError('Invalid live duration returned.')
    return route, round((time.perf_counter()-began)*1000)

def self_test():
    saved={'deadline':'2026-10-07T09:30:00-04:00','recommendation':{'departure':'2026-10-07T08:29:00-04:00','seconds':3648}}
    target=datetime.fromisoformat(saved['recommendation']['departure'])
    same=comparison(saved,3648,target)
    assert same['durationChangeSeconds']==0
    assert same['deadlineDifferenceSeconds']==-12
    assert same['liveEstimatedArrival']=='2026-10-07T09:29:48-04:00'
    slower=comparison(saved,3948,target+timedelta(seconds=2))
    assert slower['durationChangeSeconds']==300
    assert slower['scheduleDelaySeconds']==2
    assert slower['deadlineDifferenceSeconds']==290
    print('6 comparison checks passed. No waiting, browser launch or API requests.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--forecast',type=Path,default=ROOT/'.local'/'forecast-20261007T042353Z.json')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test:
        self_test()
        return
    saved,target,trip,key=load_inputs(args.forecast)
    if target <= datetime.now(timezone.utc):
        raise RuntimeError('Scheduled departure has already passed. No request made; choose a future forecast.')
    print('Waiting until '+target.strftime('%Y-%m-%d %H:%M:%S %z')+' (Toronto offset for this date).',flush=True)
    print('Keep the computer awake and this terminal open. Ctrl+C cancels.',flush=True)
    print('At that time: one live API request; Google Maps opens for manual app verification.',flush=True)
    while True:
        remaining=(target-datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining,30))
    requested_at=datetime.now(timezone.utc).astimezone(target.tzinfo)
    delay=(requested_at-target).total_seconds()
    if delay > 60:
        raise RuntimeError('Missed the target by more than one minute, possibly due to sleep. No request made.')
    maps_url='https://www.google.com/maps/dir/?'+urllib.parse.urlencode({'api':'1','origin':trip['origin'],'destination':trip['destination'],'travelmode':'driving'})
    browser_status='disabled'
    if not args.no_browser:
        try:
            browser_status='opened' if webbrowser.open(maps_url,new=2) else 'failed'
        except Exception:
            browser_status='failed'
    # Timestamp immediately before request, after browser dispatch.
    requested_at=datetime.now(timezone.utc).astimezone(target.tzinfo)
    if (requested_at-target).total_seconds()>60:
        raise RuntimeError('Browser launch delayed the check beyond one minute; no API request made.')
    route,latency_ms=fetch_live(trip,key)
    live_seconds=float(route['duration'][:-1])
    measured=comparison(saved,live_seconds,requested_at)
    original_route=saved['recommendation'].get('description','')
    current_route=route.get('description','')
    route_label_matches=bool(original_route) and original_route==current_route
    result={'scheduledAt':target.isoformat(),'requestedAt':requested_at.isoformat(),
            'completedAt':datetime.now(timezone.utc).isoformat(),'requests':1,'latencyMs':latency_ms,
            'source':'Google Routes API live estimate; Google Maps UI is manual, not scraped',
            'avoidTolls':trip.get('avoidTolls',True),'originalRoute':original_route,'liveRoute':current_route,
            'routeDescriptionMatches':route_label_matches,'browserStatus':browser_status,
            'comparison':measured,'googleMapsObservation':None,
            'limitation':'Forecast agreement, not actual driving accuracy; matching route descriptions do not prove identical paths.'}
    output=ROOT/'.local'/('live-check-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('Earlier forecast: '+str(round(measured['earlierDurationSeconds']/60,2))+' minutes')
    print('Live API estimate: '+str(round(live_seconds/60,2))+' minutes')
    print('Change: '+str(round(measured['durationChangeSeconds']/60,2))+' minutes')
    print('Live estimated arrival: '+datetime.fromisoformat(measured['liveEstimatedArrival']).strftime('%H:%M:%S %z'))
    print('Request began '+str(round(measured['scheduleDelaySeconds'],2))+' seconds after scheduled time.')
    print('Original route: '+original_route+'; live route: '+current_route)
    if not route_label_matches:
        print('Route descriptions differ or are unavailable; this may compare different routes.')
    print('Saved: '+str(output))
    print('In Maps, confirm Leave now, avoid tolls, and ON 401 Express. Record its duration and capture time separately.')
    print('The script does not automatically read the Maps screen; the saved numeric comparison comes from the API.')

if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Cancelled.')
        raise SystemExit(130)
    except Exception as error:
        if isinstance(error,(RuntimeError,FileNotFoundError,json.JSONDecodeError)):
            print('STOP: '+str(error))
        else:
            print('STOP: Unexpected '+type(error).__name__+'; secrets not displayed.')
        raise SystemExit(1)

