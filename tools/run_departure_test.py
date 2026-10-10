"""Bounded local Google traffic test. No dependencies; key is never printed."""
import json, re, time, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
MAX_REQUESTS = 48

def select_latest(rows, deadline, buffer_minutes):
    eligible = [r for r in rows if datetime.fromisoformat(r['departure']) + timedelta(seconds=r['seconds'], minutes=buffer_minutes) <= deadline]
    return max(eligible, key=lambda r: r['departure']) if eligible else None

def main():
    trip = json.loads((ROOT / '.local' / 'trip.json').read_text(encoding='utf-8'))
    deadline = datetime.fromisoformat(trip['deadline'])
    start = datetime.fromisoformat(trip['searchStart'])
    if deadline.utcoffset() is None or start.utcoffset() is None:
        raise RuntimeError('Use explicit timezone offsets.')
    if deadline <= datetime.now(timezone.utc):
        raise RuntimeError('Deadline is past. Edit .local/trip.json for a future trip.')
    if start > deadline or (deadline-start).total_seconds() > 4*3600:
        raise RuntimeError('Search must be at most four hours and end at the deadline.')
    buffer_minutes = float(trip.get('bufferMinutes', 0))
    if not 0 <= buffer_minutes <= 120:
        raise RuntimeError('Buffer must be between 0 and 120 minutes.')
    text = (ROOT / '.env.local').read_text(encoding='utf-8-sig')
    found = re.search(r'^GOOGLE_MAPS_API_KEY\s*=\s*(.*?)\s*$', text, re.M)
    key = found.group(1).strip().strip(chr(34)+chr(39)) if found else ''
    if not key or key == 'your_key_here':
        raise RuntimeError('Configure .env.local first; never share the key.')
    rows = []
    count = 0
    run_started = datetime.now(timezone.utc).isoformat()
    def query(departure):
        nonlocal count
        if departure <= datetime.now(timezone.utc):
            raise RuntimeError('Candidate departure has passed. Start a new future test.')
        if count >= MAX_REQUESTS:
            raise RuntimeError('Request cap reached; no further calls made.')
        count += 1
        body = {'origin':{'address':trip['origin']}, 'destination':{'address':trip['destination']},
                'travelMode':'DRIVE', 'routingPreference':'TRAFFIC_AWARE_OPTIMAL', 'trafficModel':'BEST_GUESS',
                'departureTime':departure.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                'computeAlternativeRoutes':False, 'routeModifiers':{'avoidTolls':trip.get('avoidTolls',True),'avoidHighways':False,'avoidFerries':True}, 'units':'METRIC'}
        request = urllib.request.Request('https://routes.googleapis.com/directions/v2:computeRoutes',
            data=json.dumps(body).encode(), method='POST', headers={'Content-Type':'application/json',
            'X-Goog-Api-Key':key, 'X-Goog-FieldMask':'routes.duration,routes.distanceMeters,routes.description,routes.legs.startLocation,routes.legs.endLocation,fallbackInfo'})
        began = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError('Google returned HTTP '+str(error.code)+'. Check API enablement, key restrictions and billing; key not displayed.') from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Connection failed or timed out. No automatic retries.') from None
        if data.get('fallbackInfo'):
            raise RuntimeError('Provider returned fallback routing; stop rather than use it as a full traffic forecast.')
        routes = data.get('routes',[])
        if not routes:
            raise RuntimeError('No route returned.')
        route = routes[0]
        duration = route.get('duration','')
        if not re.fullmatch(r'\d+(?:\.\d+)?s',duration):
            raise RuntimeError('Provider returned an invalid duration.')
        row = {'departure':departure.isoformat(), 'seconds':float(duration[:-1]),
               'distanceMeters':route.get('distanceMeters'), 'description':route.get('description',''),
               'startLocation':route.get('legs',[{}])[0].get('startLocation'),
               'endLocation':route.get('legs',[{}])[-1].get('endLocation'),
               'fetchedAt':datetime.now(timezone.utc).isoformat(), 'latencyMs':round((time.perf_counter()-began)*1000)}
        rows.append(row)
        print(departure.strftime('%H:%M')+' -> '+str(round(row['seconds']/60,1))+' minutes', flush=True)
        return row
    # Coarse scan does not assume travel durations are monotonic.
    candidate = start
    while candidate <= deadline:
        if candidate > datetime.now(timezone.utc)+timedelta(seconds=30):
            query(candidate)
        candidate += timedelta(minutes=5)
    chosen = select_latest(rows,deadline,buffer_minutes)
    if chosen:
        coarse_departure = datetime.fromisoformat(chosen['departure'])
        for minute in range(1,5):
            candidate = coarse_departure+timedelta(minutes=minute)
            if candidate <= deadline:
                query(candidate)
        chosen = select_latest(rows,deadline,buffer_minutes)
    result = {'forecastRunStarted':run_started,'deadline':trip['deadline'],'avoidTolls':trip.get('avoidTolls',True),
              'bufferMinutes':buffer_minutes,'requests':count,'recommendation':chosen,
              'scope':'Latest evaluated departure; five-minute scan with one-minute refinement near the latest feasible coarse sample. Unsampled short feasible windows may be missed.', 'candidates':rows}
    output = ROOT / '.local' / ('forecast-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    if chosen:
        departure = datetime.fromisoformat(chosen['departure'])
        arrival = departure+timedelta(seconds=chosen['seconds'])
        print('RECOMMENDED DEPARTURE: '+departure.strftime('%Y-%m-%d %H:%M %z'))
        print('ESTIMATED DRIVING ARRIVAL: '+arrival.strftime('%H:%M:%S %z'))
        print('ROUTE: '+chosen['description'])
        print('Forecast only. Follow the returned route; allow separately for parking/walking.')
    else:
        print('No evaluated departure meets the deadline; do not assume the trip is impossible outside this search window.')
    print('Requests: '+str(count)+'; actual billed cost is not verified.')
    print('Saved: '+str(output))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if isinstance(error, (RuntimeError,FileNotFoundError,json.JSONDecodeError)):
            print('STOP: '+str(error))
        else:
            print('STOP: Unexpected '+type(error).__name__+'; no secret values displayed.')
        raise SystemExit(1)

