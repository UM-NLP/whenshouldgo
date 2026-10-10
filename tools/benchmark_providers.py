"""Local forecast stability benchmark; never a measurement of actual arrival accuracy."""
import argparse, json, math, re, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, quote

ROOT = Path(__file__).resolve().parent.parent
def now(): return datetime.now(timezone.utc)
def timestamp(value):
    try: result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError): raise RuntimeError('Invalid ISO date/time.') from None
    if result.utcoffset() is None: raise RuntimeError('Explicit timezone offset required.')
    return result
def seconds(value):
    if isinstance(value, bool): raise RuntimeError('Invalid duration.')
    try: value = float(value)
    except (ValueError, TypeError): raise RuntimeError('Missing or invalid duration.') from None
    if not math.isfinite(value) or value <= 0: raise RuntimeError('Invalid duration.')
    return value
def keys():
    text = (ROOT/'.env.local').read_text(encoding='utf-8-sig')
    result = {}
    for provider, name in [('google','GOOGLE_MAPS_API_KEY'),('tomtom','TOMTOM_API_KEY')]:
        match = re.search(r'^'+name+r'\s*=\s*(.*?)\s*$',text,re.M)
        value = match.group(1).strip().strip(chr(34)+chr(39)) if match else ''
        if not value or value == 'your_key_here': raise RuntimeError('Configure '+name+' locally; never share the key.')
        result[provider] = value
    return result
def save(prefix, data):
    directory = ROOT/'.local'
    directory.mkdir(exist_ok=True)
    path = directory/(prefix+'-'+now().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    path.write_text(json.dumps(data,indent=2),encoding='utf-8')
    print('Saved: '+str(path))
    return path
class Client:
    def __init__(self, credentials, limits=None):
        self.credentials=credentials
        self.count={'google':0,'tomtom':0}
        self.limits=limits or {'google':50,'tomtom':6}
    def request(self, provider, url, body=None, headers=None):
        if self.count[provider]>=self.limits[provider]: raise RuntimeError(provider+' request cap reached.')
        self.count[provider]+=1
        req=urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        try:
            with urllib.request.urlopen(req,timeout=25) as response: return json.load(response)
        except urllib.error.HTTPError as error: raise RuntimeError(provider+' HTTP '+str(error.code)+'; check key, API access and billing. No retry.') from None
        except (urllib.error.URLError,TimeoutError,OSError): raise RuntimeError(provider+' connection failed. No retry; URL and key hidden.') from None
        except (ValueError,TypeError): raise RuntimeError(provider+' invalid JSON response.') from None
    def geocode(self,address):
        url='https://api.tomtom.com/search/2/geocode/'+quote(address,safe='')+'.json?'+urlencode({'key':self.credentials['tomtom'],'countrySet':'CA','limit':1})
        results=self.request('tomtom',url).get('results',[])
        if not results: raise RuntimeError('No geocoding match.')
        r=results[0]; pos=r.get('position',{})
        lat=float(pos['lat']); lon=float(pos['lon'])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180: raise RuntimeError('Invalid geocode coordinates.')
        return {'input':address,'address':r.get('address',{}),'position':{'lat':lat,'lon':lon},'score':r.get('matchConfidence',{}).get('score'),'type':r.get('type'),'warning':'Verify house number, street, city and postal code. Score is provider-specific, not a confidence percentage.'}
    def route(self,provider,locations,departure=None,arrival=None):
        began=now(); timer=time.perf_counter()
        if provider=='google':
            body={'origin':{'location':{'latLng':{'latitude':locations[0]['position']['lat'],'longitude':locations[0]['position']['lon']}}},'destination':{'location':{'latLng':{'latitude':locations[1]['position']['lat'],'longitude':locations[1]['position']['lon']}}},'travelMode':'DRIVE','routingPreference':'TRAFFIC_AWARE_OPTIMAL','trafficModel':'BEST_GUESS','computeAlternativeRoutes':False,'routeModifiers':{'avoidTolls':True,'avoidFerries':True}}
            if departure and departure<=now(): raise RuntimeError('Forecast departure has passed.')
            if departure: body['departureTime']=departure.astimezone(timezone.utc).isoformat()
            data=self.request(provider,'https://routes.googleapis.com/directions/v2:computeRoutes',body,{'Content-Type':'application/json','X-Goog-Api-Key':self.credentials[provider],'X-Goog-FieldMask':'routes.duration,routes.distanceMeters,routes.description,routes.polyline.encodedPolyline,fallbackInfo'})
            if data.get('fallbackInfo'): raise RuntimeError('Google traffic fallback rejected.')
            if not data.get('routes'): raise RuntimeError('Google returned no route.')
            route=data['routes'][0]; duration=route.get('duration','')
            if not re.fullmatch(r'\d+(?:\.\d+)?s',duration): raise RuntimeError('Invalid Google duration.')
            duration=seconds(duration[:-1]); actual_departure=departure or began
            geometry=route.get('polyline'); summary={'description':route.get('description'),'distanceMeters':route.get('distanceMeters')}
        else:
            positions=':'.join(str(p['position']['lat'])+','+str(p['position']['lon']) for p in locations)
            params={'key':self.credentials[provider],'traffic':'true','travelMode':'car','routeType':'fastest','avoid':'tollRoads','computeTravelTimeFor':'all','routeRepresentation':'polyline'}
            if departure and departure<=now(): raise RuntimeError('Forecast departure has passed.')
            if departure: params['departAt']=departure.isoformat()
            elif arrival: params['arriveAt']=arrival.isoformat()
            else: params['departAt']='now'
            data=self.request(provider,'https://api.tomtom.com/routing/1/calculateRoute/'+positions+'/json?'+urlencode(params))
            if not data.get('routes'): raise RuntimeError('TomTom returned no route.')
            route=data['routes'][0]; summary=route.get('summary',{}); duration=seconds(summary.get('travelTimeInSeconds'))
            actual_departure=timestamp(summary['departureTime'])
            geometry=[leg.get('points',[]) for leg in route.get('legs',[])]
        if not geometry: raise RuntimeError(provider+' missing route geometry.')
        return {'provider':provider,'departure':actual_departure.isoformat(),'seconds':duration,'arrival':(actual_departure+timedelta(seconds=duration)).isoformat(),'requestStarted':began.isoformat(),'latencyMs':round((time.perf_counter()-timer)*1000),'summary':summary,'geometry':geometry}
def latest(rows,deadline,buffer):
    eligible=[r for r in rows if timestamp(r['departure'])+timedelta(seconds=r['seconds'],minutes=buffer)<=deadline]
    return max(eligible,key=lambda r:timestamp(r['departure'])) if eligible else None
def wait_delay(target,current):
    delay=(current-target).total_seconds()
    if delay>60: raise RuntimeError('Scheduled target missed by over 60 seconds. No substitute observation.')
    return max(0,(target-current).total_seconds())
def comparison(early,live,target,deadline):
    return {'earlyMinutes':early['seconds']/60,'liveMinutes':live['seconds']/60,'changeMinutes':(live['seconds']-early['seconds'])/60,'delaySeconds':(timestamp(live['requestStarted'])-target).total_seconds(),'liveArrival':live['arrival'],'deadlineMissMinutes':max(0,(timestamp(live['arrival'])-deadline).total_seconds()/60),'routeWarning':'Providers and updated requests may select different routes; geometry retained. Forecast stability, not ground truth.'}
def plan(args):
    deadline=timestamp(args.deadline)
    if deadline<=now(): raise RuntimeError('Deadline must be future.')
    start=deadline.replace(hour=6,minute=0,second=0,microsecond=0)
    if not 0<(deadline-start).total_seconds()<=4*3600: raise RuntimeError('Deadline must be after 06:00 and no later than 10:00 in supplied offset.')
    if int((deadline-start).total_seconds()//300)+1+4+2>50: raise RuntimeError('Search window would exceed 50 Google requests. Use deadline no later than 09:30.')
    if start<=now()+timedelta(minutes=2): raise RuntimeError('Start the plan before 05:58 in the supplied offset.')
    if not 0<=args.buffer<=120: raise RuntimeError('Buffer must be 0â€“120 minutes.')
    trip=json.loads((ROOT/'.local/trip.json').read_text(encoding='utf-8'))
    client=Client(keys())
    if not args.confirm_locations:
        locations=[client.geocode(trip[k]) for k in ('origin','destination')]
        for label,location in zip(('Origin','Destination'),locations):
            print(label+': '+location['address'].get('freeformAddress','Unknown address'))
            if location['score'] is None or location['score']<0.8 or location['type']!='Point Address': print('CAUTION: Match needs close review; confidence low/missing or not an exact address.')
            print('Type: '+str(location['type'])+'; score: '+str(location['score'])+'. '+location['warning'])
        path=save('benchmark-locations',{'locations':locations})
        print('Review matches, then rerun with --confirm-locations "'+str(path)+'" and the same explicit --deadline.')
        return
    locations=json.loads(Path(args.confirm_locations).read_text(encoding='utf-8'))['locations']
    if len(locations)!=2: raise RuntimeError('Exactly two confirmed locations required.')
    for location in locations:
        lat=float(location['position']['lat']); lon=float(location['position']['lon'])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180: raise RuntimeError('Invalid confirmed coordinates.')
    if [p['input'] for p in locations]!=[trip[k] for k in ('origin','destination')]: raise RuntimeError('Confirmed locations do not match trip.')
    target=deadline-timedelta(minutes=args.buffer)
    tomtom=client.route('tomtom',locations,arrival=target)
    if timestamp(tomtom['departure'])<=now()+timedelta(minutes=2): raise RuntimeError('TomTom departure too soon or past.')
    if timestamp(tomtom['arrival'])>target+timedelta(seconds=1): raise RuntimeError('TomTom arrival route misses target.')
    rows=[]; candidate=start
    while candidate<=deadline:
        rows.append(client.route('google',locations,departure=candidate))
        print(candidate.strftime('%H:%M')+' Google '+str(round(rows[-1]['seconds']/60,1))+' min',flush=True)
        candidate+=timedelta(minutes=5)
    selected=latest(rows,deadline,args.buffer)
    if not selected: raise RuntimeError('No Google candidate meets target in evaluated window.')
    coarse=timestamp(selected['departure'])
    for minute in range(1,5):
        candidate=coarse+timedelta(minutes=minute)
        if candidate<=deadline: rows.append(client.route('google',locations,departure=candidate))
    selected=latest(rows,deadline,args.buffer)
    recommendations={'google':selected,'tomtom':tomtom}
    events=[]
    for dep in sorted(set(r['departure'] for r in recommendations.values()),key=timestamp):
        departure=timestamp(dep)
        early={}
        for provider in ('google','tomtom'):
            early[provider]=client.route(provider,locations,departure=departure)
        events.append({'target':dep,'early':early})
    result={'schema':1,'createdAt':now().isoformat(),'deadline':deadline.isoformat(),'bufferMinutes':args.buffer,'locations':locations,'avoidTolls':True,'recommendations':recommendations,'events':events,'requests':client.count,'googleCandidates':rows,'scope':'Latest evaluated Google candidate; narrow unsampled feasible windows may be missed. Provider-selected routes can differ. No actual arrival observations.'}
    plan_path=save('benchmark-plan',result)
    print('Watch command: py '+chr(34)+str(Path(__file__).resolve())+chr(34)+' --watch '+chr(34)+str(plan_path)+chr(34))
    for provider,row in recommendations.items(): print(provider+' leave '+row['departure']+'; estimated arrival '+row['arrival'])
    print('Keep computer awake. Charges not verified.')
def watch(path):
    plan=json.loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('schema')!=1 or len(plan.get('events',[])) not in (1,2): raise RuntimeError('Unsupported plan.')
    events=sorted(plan['events'],key=lambda e:timestamp(e['target']))
    for event in events:
        if timestamp(event['target'])<=now(): raise RuntimeError('Watch target already passed. Plan a new future test.')
        for provider in ('google','tomtom'): seconds(event['early'][provider]['seconds'])
        wait_delay(timestamp(event['target']),now())
    client=Client(keys(),{'google':2,'tomtom':2}); observed=[]
    for event in events:
        target=timestamp(event['target'])
        print('Waiting until '+target.isoformat(),flush=True)
        while True:
            try: delay=wait_delay(target,now())
            except RuntimeError:
                observed.append({'target':event['target'],'error':'Scheduled target missed by over 60 seconds; no calls made.'})
                save('benchmark-live',{'planFile':str(Path(path).resolve()),'observations':observed,'requests':client.count})
                return
            if not delay: break
            time.sleep(min(delay,30))
        def fetch(provider):
            try:
                row=client.route(provider,plan['locations'])
                return {'live':row,'comparison':comparison(event['early'][provider],row,target,timestamp(plan['deadline']))}
            except Exception as error:
                return {'error':str(error) if isinstance(error,RuntimeError) else 'Unexpected '+type(error).__name__+'; details hidden.'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=dict(zip(('google','tomtom'),pool.map(fetch,('google','tomtom'))))
        observed.append({'target':event['target'],'results':results})
        save('benchmark-live',{'planFile':str(Path(path).resolve()),'observations':observed,'requests':client.count})
        for provider,result in results.items():
            if 'comparison' in result:
                c=result['comparison']; print(provider+': early '+str(round(c['earlyMinutes'],2))+' min; live '+str(round(c['liveMinutes'],2))+' min; change '+str(round(c['changeMinutes'],2))+' min; arrival '+c['liveArrival'])
            else: print(provider+': '+result['error'])
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deadline',help='Explicit future ISO datetime with offset')
    parser.add_argument('--buffer',type=float,default=0)
    parser.add_argument('--confirm-locations',help='Reviewed benchmark-locations JSON')
    parser.add_argument('--watch',help='Saved benchmark-plan JSON; does not re-plan')
    args=parser.parse_args()
    if args.watch: watch(args.watch)
    elif args.deadline: plan(args)
    else: parser.error('Supply --deadline or --watch.')
if __name__=='__main__':
    try: main()
    except Exception as error:
        print('STOP: '+(str(error) if isinstance(error,RuntimeError) else 'Unexpected '+type(error).__name__+'; details hidden.'))
        raise SystemExit(1)

