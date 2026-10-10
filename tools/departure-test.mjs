export function selectLatest(rows, deadlineMs, bufferMinutes = 0) {
  if (!Number.isFinite(deadlineMs) || !Number.isFinite(bufferMinutes) || bufferMinutes < 0) throw new Error('Invalid deadline or buffer');
  const feasible = rows.filter(r => Number.isFinite(r.departureMs) && Number.isFinite(r.seconds) && r.seconds >= 0 && r.departureMs + r.seconds * 1000 + bufferMinutes * 60000 <= deadlineMs);
  return feasible.sort((a,b) => b.departureMs - a.departureMs)[0] || null;
}
export function candidateTimes(startMs, endMs, stepMinutes) {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs || !Number.isFinite(stepMinutes) || stepMinutes <= 0) throw new Error('Invalid search window');
  const result = [];
  for (let t = startMs; t <= endMs; t += stepMinutes * 60000) { if (result.length >= 1000) throw new Error('Window too large'); result.push(t); }
  return result;
}
export async function evaluate(times, query, rows = []) {
  for (const departureMs of times) rows.push({departureMs, ...await query(departureMs)});
  return rows;
}
export function createGoogleQuery({key, origin, destination, avoidTolls = true, maxRequests = 48, fetchImpl = fetch}) {
  let requests = 0;
  async function query(departureMs) {
    if (departureMs <= Date.now()) throw new Error('Departure must be in the future');
    if (requests >= maxRequests) throw new Error('Request cap reached');
    requests++;
    const started = performance.now();
    let response;
    try {
      response = await fetchImpl('https://routes.googleapis.com/directions/v2:computeRoutes', {
        method:'POST', signal:AbortSignal.timeout(20000),
        headers:{'Content-Type':'application/json','X-Goog-Api-Key':key,'X-Goog-FieldMask':'routes.duration,routes.distanceMeters,routes.description,routes.routeLabels,routes.legs.startLocation,routes.legs.endLocation,fallbackInfo'},
        body:JSON.stringify({origin:{address:origin},destination:{address:destination},travelMode:'DRIVE',routingPreference:'TRAFFIC_AWARE_OPTIMAL',trafficModel:'BEST_GUESS',departureTime:new Date(departureMs).toISOString(),computeAlternativeRoutes:false,routeModifiers:{avoidTolls,avoidHighways:false,avoidFerries:true},units:'METRIC'})
      });
    } catch { throw new Error('Traffic request failed or timed out; no automatic retries'); }
    if (!response.ok) { let status = ''; try { status = (await response.json()).error?.status || ''; } catch {} throw new Error('Routes API HTTP '+response.status+' '+status); }
    const body = await response.json();
    if (body.fallbackInfo) throw new Error('Provider used fallback; stop rather than treat it as a full traffic forecast');
    const route = body.routes?.[0];
    if (!route) throw new Error('No route available');
    if (!/^\d+(?:\.\d+)?s$/.test(route.duration)) throw new Error('Invalid duration from provider');
    return {seconds:Number(route.duration.slice(0,-1)),distanceMeters:route.distanceMeters,description:route.description || '',startLocation:route.legs?.[0]?.startLocation,endLocation:route.legs?.at(-1)?.endLocation,latencyMs:Math.round(performance.now()-started),fetchedAt:new Date().toISOString()};
  }
  return {query, requestCount:()=>requests};
}

