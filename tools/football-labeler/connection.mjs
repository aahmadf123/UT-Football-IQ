/** Serialized revision-bound local saves. Server conflicts never silently overwrite a tab. */
export class DraftConnection {
  constructor(session, request = (...args) => fetch(...args)) { this.session = session; this.request = request; this.states = new Map(); }
  key(source, reviewer, pass) { return new URLSearchParams({source,reviewer,pass}).toString(); }
  async read(source, reviewer, pass) {
    const key=this.key(source,reviewer,pass);
    await this.states.get(key)?.queue.catch(()=>{});
    const response=await this.request(`/api/draft?${key}`,{cache:'no-store'});
    const value=await response.json(); if (!response.ok) throw Error(value.detail || 'Saved draft could not be loaded.');
    this.states.set(key,{revision:value.revision,queue:Promise.resolve(),conflict:false}); return value.draft;
  }
  save(draft) {
    const snapshot=structuredClone(draft), key=this.key(snapshot.source_id,snapshot.reviewer_id,snapshot.review_pass), state=this.states.get(key);
    if (!state) return Promise.reject(Error('Load this reviewer/pass before saving to the local folder.'));
    const operation=state.queue.catch(()=>{}).then(async () => {
      if (state.conflict) throw Error('Saved draft changed in another tab. Export a recovery copy, then reload saved draft.');
      const response=await this.request(`/api/draft?${key}`,{method:'PUT',headers:{'Content-Type':'application/json','X-Labeler-Token':this.session.token},body:JSON.stringify({revision:state.revision,draft:snapshot})});
      const result=await response.json();
      if (!response.ok) { if (response.status===409) state.conflict=true; throw Error(result.detail || 'Local save failed. Export a recovery copy.'); }
      state.revision=result.revision; return result;
    });
    state.queue=operation; return operation;
  }
}

export function detectionProposals(value, source) {
  const finite=x=>typeof x==='number' && Number.isFinite(x);
  if (!value || value.schema_version!==1 || value.evidence_kind!=='detection_proposals' || value.source_id!==source.id || !/^[a-f0-9]{64}$/.test(value.media_sha256) || value.media_sha256!==source.media_sha256 || value.width!==source.width || value.height!==source.height || !Array.isArray(value.frames)) throw Error('Proposal source ID, full media hash and dimensions must match this original.');
  const ids=new Set(); let last=-Infinity;
  for (const frame of value.frames) {
    if (!finite(frame.timestamp_seconds) || frame.timestamp_seconds<0 || frame.timestamp_seconds>source.duration_seconds || frame.timestamp_seconds<=last || !Array.isArray(frame.detections)) throw Error('Proposal frames need increasing actual source timestamps.');
    last=frame.timestamp_seconds;
    for (const detection of frame.detections) {
      const b=detection.bbox;
      if (typeof detection.proposal_id!=='string' || !detection.proposal_id || ids.has(detection.proposal_id) || !Array.isArray(b) || b.length!==4 || !b.every(finite) || !(0<=b[0]&&b[0]<b[2]&&b[2]<=source.width&&0<=b[1]&&b[1]<b[3]&&b[3]<=source.height) || !['player','referee','unknown'].includes(detection.actor_kind) || (detection.confidence!=null && (!finite(detection.confidence)||detection.confidence<0||detection.confidence>1))) throw Error('Each hint needs a unique proposal ID, valid native-pixel box and unreviewed actor kind.');
      ids.add(detection.proposal_id);
    }
  }
  return structuredClone(value);
}
