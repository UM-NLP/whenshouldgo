import assert from 'node:assert/strict';
import {selectLatest, candidateTimes, createGoogleQuery} from './departure-test.mjs';
const t = Date.parse('2026-10-07T11:00:00Z');
assert.equal(selectLatest([{departureMs:t,seconds:5400},{departureMs:t+900000,seconds:4500}],t+5400000).departureMs,t+900000);
assert.equal(selectLatest([{departureMs:t,seconds:5400}],t+5400000,10),null);
assert.equal(selectLatest([],t),null);
assert.equal(candidateTimes(t,t+600000,5).length,3);
assert.throws(()=>candidateTimes(t,t+600000,0));
assert.equal(Date.parse('2026-10-07T09:30:00-04:00'),Date.parse('2026-10-07T13:30:00Z'));
const fake = createGoogleQuery({key:'synthetic',origin:'A',destination:'B',fetchImpl:async()=>({ok:false,status:403,json:async()=>({error:{status:'PERMISSION_DENIED'}})})});
await assert.rejects(fake.query(Date.now()+600000),/403 PERMISSION_DENIED/);

