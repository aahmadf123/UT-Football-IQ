import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DraftConnection, detectionProposals } from './connection.mjs';

test('local saves serialize revisions and snapshot mutable draft content',async()=>{
  const calls=[];
  const request=async(url,options)=>{ if(!options?.method)return{ok:true,json:async()=>({draft:null,revision:null})};const body=JSON.parse(options.body);calls.push(body);return{ok:true,json:async()=>({revision:`r${calls.length}`})}; };
  const store=new DraftConnection({token:'local-test-only'},request);
  await store.read('source','staff','primary');
  const draft={source_id:'source',reviewer_id:'staff',review_pass:'primary',plays:[]};
  const first=store.save(draft);draft.plays.push({id:'later'});const second=store.save(draft);
  await Promise.all([first,second]);
  assert.equal(calls[0].revision,null);assert.equal(calls[0].draft.plays.length,0);
  assert.equal(calls[1].revision,'r1');assert.equal(calls[1].draft.plays.length,1);
});
test('conflicting tabs stop saves until an explicit server reload',async()=>{
  let saves=0;
  const store=new DraftConnection({token:'local-test-only'},async(url,options)=>{if(!options?.method)return{ok:true,json:async()=>({draft:null,revision:'current'})};saves++;return{ok:false,status:409,json:async()=>({detail:'Other tab saved; recover and reload.'})};});
  const draft={source_id:'source',reviewer_id:'staff',review_pass:'primary'};
  await store.read('source','staff','primary');await assert.rejects(store.save(draft),/Other tab/);await assert.rejects(store.save(draft),/another tab/);assert.equal(saves,1);
  await store.read('source','staff','primary');await assert.rejects(store.save(draft));assert.equal(saves,2);
});
test('proposal hints bind exact media/dimensions and never become annotations',()=>{
  const source={id:'s',media_sha256:'a'.repeat(64),width:100,height:80,duration_seconds:4};
  const value={schema_version:1,evidence_kind:'detection_proposals',source_id:'s',media_sha256:source.media_sha256,width:100,height:80,frames:[{timestamp_seconds:1,detections:[{proposal_id:'p',bbox:[1,2,20,40],actor_kind:'referee'}]}]};
  assert.equal(detectionProposals(value,source).evidence_kind,'detection_proposals');
  assert.equal(value.reviewed,undefined);
  assert.throws(()=>detectionProposals({...value,media_sha256:'b'.repeat(64)},source),/hash/);
  assert.throws(()=>detectionProposals({...value,frames:[...value.frames,...value.frames]},source),/increasing/);
});
