import { CATEGORIES, PASSES, compareDrafts, createDraft, normalizeDraft, putPoint, validateDraft, validatePlay } from './annotations.mjs';
import { DraftConnection, detectionProposals } from './connection.mjs';

const $ = (id) => document.getElementById(id);
const video = $('video'), canvas = $('boxes'), ctx = canvas.getContext('2d');
let catalog = { schema_version: 1, sources: [] }, source = null, draft = null, selectedId = null;
let objectUrl = null, mediaReady = false, presented = null, drag = null;
let requestedSeek=null;
let frameIndex = null;
let connection = null, session = null, loadingGeneration = 0, proposals = null, saveVersion = 0;
let formDirty=false;
const status = (message) => { $('status').textContent = message; };
const error = (message = '') => { $('error').hidden = !message; $('error').textContent = message; };
const safely = (fn) => async (...args) => { try { error(); await fn(...args); } catch (cause) { error(cause.message ?? String(cause)); } };
const filename = (value) => value.split(/[\\/]/).at(-1).normalize('NFC').toLowerCase();
const localKey = (identity) => `footiq-annotation-draft-v1:${identity?.source_id??source.id}:${identity?.reviewer_id??$('reviewer').value.trim()}:${identity?.review_pass??$('pass').value}`;
const selected = () => draft?.plays.find((play) => play.id === selectedId);
const duration = () => Number.isFinite(video.duration) ? video.duration : source?.duration_seconds ?? Infinity;
const fps = () => {
  const value = source?.fps ?? source?.nominal_fps ?? source?.r_frame_rate;
  if (typeof value === 'string' && value.includes('/')) { const [a, b] = value.split('/').map(Number); if (a > 0 && b > 0) return a / b; }
  return typeof value === 'number' && value > 0 ? value : 30;
};
const clock = () => presented ?? video.currentTime; // UI fallback only; annotation capture requires a presented PTS.
function seekTo(time){requestedSeek=time;presented=null;video.currentTime=time;}

function persist() {
  if (!draft || !source || !draft.reviewer_id) return;
  draft.updated_at = new Date().toISOString();
  try { localStorage.setItem(localKey(draft), JSON.stringify(draft)); }
  catch { status('Browser draft storage is full or unavailable. Export a JSON backup now.'); }
  updateProgress();
  if (connection) {
    const current=++saveVersion;
    $('save-state').textContent='Saving draft to local folder…';
    connection.save(draft).then(() => { if(current===saveVersion) $('save-state').textContent='Saved to local folder · unreviewed draft'; }).catch(cause=>{ if(current===saveVersion) { $('save-state').textContent='Not saved to folder · export a recovery copy'; error(cause.message); } });
  } else $('save-state').textContent='Browser recovery only · export a JSON backup';
}
function updateProgress() {
  const plays=draft?.plays??[], tracks=plays.flatMap(p=>p.dense_tracks??[]), points=tracks.reduce((n,t)=>n+t.points.length,0);
  $('progress').textContent=source?`${source.filename} · ${plays.length} saved play windows · ${tracks.length} targets · ${points} manually saved boxes · ${plays.reduce((n,p)=>n+(p.events?.length??0),0)} events. No review completion is implied.`:'No source selected.';
}
function download(name, value) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function requireMedia() {
  if (!draft || !source) throw Error('Select a catalog source first.');
  if (!$('reviewer').value.trim()) throw Error('Enter your reviewer ID first.');
  if(draft.reviewer_id!==$('reviewer').value.trim() || draft.review_pass!==$('pass').value) throw Error('Wait for the selected reviewer/pass to load before editing. Existing annotations retain their original reviewer.');
  if (!mediaReady || video.seeking || video.readyState < 2) throw Error('Open the matching original film and wait for the presented frame.');
}
function requireEdit() {
  requireMedia();
  if (!video.requestVideoFrameCallback) throw Error('Frame annotation requires a browser with presented-frame timestamps. Use current Chrome or Edge; requested seek time is not a verified frame timestamp.');
  if (presented == null) throw Error('Wait for the sought frame to be presented before recording an observation.');
}
function requirePlay() { requireEdit(); const play = selected(); if (!play) throw Error('Save and select a play first.'); return play; }
function sourceForm() {
  $('category').value = source.category ?? ''; $('group').value = source.group_id ?? '';
  $('confirmed').checked = source.category_status === 'confirmed' && source.group_status === 'confirmed';
}
async function loadPass() {
  if (!source) return;
  if(formDirty && !confirm('Boundary or concept fields have unsaved changes. Discard those fields and open another reviewer/pass?')) { $('reviewer').value=draft?.reviewer_id??'';$('pass').value=draft?.review_pass??'primary';return; }
  const current=++loadingGeneration, selectedSource=source, reviewer=$('reviewer').value.trim(), pass=$('pass').value;
  ++saveVersion;
  try{localStorage.setItem('footiq-labeler-workspace',JSON.stringify({source:source.id,reviewer,pass}));}catch{/* Draft exports remain available without browser storage. */}
  let cached=null;
  try { cached=localStorage.getItem(localKey()); } catch { status('Browser recovery storage is unavailable; use local-folder saves and exports.'); }
  draft=null;selectedId=null;render();
  $('save-state').textContent='Loading reviewer/pass…';
  let saved=null;
  if(connection && reviewer) { try{saved=await connection.read(source.id,reviewer,pass);}catch(cause){$('save-state').textContent='Saved draft could not be loaded · retry Reload saved draft';throw cause;} }
  if(current!==loadingGeneration || source!==selectedSource) return;
  draft = saved ? normalizeDraft(saved,source) : cached ? normalizeDraft(JSON.parse(cached), source) : createDraft(source, reviewer, pass);
  if(draft.reviewer_id!==reviewer || draft.review_pass!==pass) throw Error('Saved workspace identity differs from this reviewer/pass. Import its draft explicitly.');
  // The selected catalog controls source grouping. An older catalog cannot
  // silently inherit a previous local draft's confirmation.
  if(!saved) Object.assign(draft, { source_category: source.category ?? null, source_group_id: source.group_id ?? '', source_context_confirmed: source.group_status === 'confirmed' && source.category_status === 'confirmed' });
  if(saved) { $('category').value=draft.source_category??''; $('group').value=draft.source_group_id??''; $('confirmed').checked=draft.source_context_confirmed===true; }
  selectedId = null; $('comparison').textContent = ''; newPlay(); render();
  $('save-state').textContent=saved?'Loaded saved local draft':cached?'Recovered browser draft · save to local folder':reviewer?'New reviewer/pass · no saved annotations':'Enter your reviewer ID to open a draft';
  status(`Source ${source.id} · ${draft.plays.length} draft plays · media hash ${source.media_sha256 ? 'recorded; verify at freeze' : 'pending streaming verification'}.`);
}
function newPlay() {
  formDirty=false;$('form-state').textContent='';
  selectedId = null;
  let number = (draft?.plays.length ?? 0) + 1;
  while (draft?.plays.some((play) => play.id === `${source.id}-p${String(number).padStart(3, '0')}`)) number++;
  $('play-id').value = source ? `${source.id}-p${String(number).padStart(3, '0')}` : '';
  $('canonical-id').value = ''; $('start').value = ''; $('snap').value = ''; $('end').value = ''; $('notes').value = '';
  $('target').value = ''; $('player-id').value = ''; $('entity-id').value = ''; $('readable').checked = false; $('team-resolved').checked = false;
  $('actor-kind').value='unknown'; $('actor-side').value='unknown';
  for(const id of ['practice-period','offense-concept','defense-concept']) $(id).value='';
  $('plays').value = ''; renderTracks(); updateFrame();
}
function choosePlay(id) {
  const play = draft.plays.find((item) => item.id === id);
  if (!play) return;
  selectedId = id;
  for (const [key, field] of [['play-id', 'id'], ['canonical-id', 'canonical_play_id'], ['start', 'start_seconds'], ['snap', 'snap_seconds'], ['end', 'end_seconds'], ['notes', 'notes']]) $(key).value = play[field] ?? '';
  for(const [id,field] of [['practice-period','practice_period'],['offense-concept','offense_concept'],['defense-concept','defense_concept']]) $(id).value=play[field]??'';
  if (mediaReady) seekTo(play.start_seconds);
  $('plays').value = id; renderTracks(); updateFrame();
}
function render() {
  $('plays').replaceChildren();
  for (const play of draft?.plays ?? []) {
    const option = new Option(`${play.id} · ${play.start_seconds.toFixed(3)}–${play.end_seconds.toFixed(3)}s · snap ${play.snap_seconds == null ? 'unknown' : play.snap_seconds.toFixed(3)}`, play.id);
    $('plays').append(option);
  }
  $('plays').value = selectedId ?? ''; renderTracks(); updateFrame(); updateProgress();
}
function renderTracks() {
  $('track-rows').replaceChildren(); $('events').replaceChildren(); $('visibility-events').replaceChildren();
  const play = selected();
  for (const track of play?.dense_tracks ?? []) {
    const row = document.createElement('tr');
    const identity = play.identities?.find((item) => item.target_id === track.target_id);
    for (const value of [`${track.target_id} · ${track.actor_kind??'unknown'} / ${track.side??'unknown'}`, track.points.length, track.visibility_events?.length ?? 0, identity?.player_id || 'Unknown']) {
      const cell = document.createElement('td'); cell.textContent = String(value); row.append(cell);
    }
    row.tabIndex = 0; row.setAttribute('aria-label', `Select track ${track.target_id}`);
    const choose = () => {
      $('target').value = track.target_id; $('player-id').value = identity?.player_id ?? ''; $('entity-id').value = identity?.canonical_entity_id ?? '';
      $('readable').checked = identity?.readable ?? false; $('team-resolved').checked = identity?.team_resolved ?? false; updateFrame();
      $('actor-kind').value=track.actor_kind??'unknown'; $('actor-side').value=track.side??'unknown';
    };
    row.addEventListener('click', choose); row.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(); } });
    $('track-rows').append(row);
    (track.visibility_events ?? []).forEach((event, index) => {
      const item = document.createElement('li'); item.textContent = `${track.target_id} · ${event.visibility_state} · ${event.timestamp_seconds.toFixed(6)} s`;
      const remove = document.createElement('button'); remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${track.target_id} visibility at ${event.timestamp_seconds.toFixed(6)} seconds`);
      remove.onclick = () => { track.visibility_events.splice(index, 1); persist(); renderTracks(); updateFrame(); }; item.append(remove); $('visibility-events').append(item);
    });
  }
  (play?.events ?? []).forEach((event, index) => {
    const item = document.createElement('li'); item.textContent = `${event.event_type} · ${event.timestamp_seconds.toFixed(3)} s`;
    const remove = document.createElement('button'); remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${event.event_type} at ${event.timestamp_seconds.toFixed(3)} seconds`);
    remove.onclick = () => { play.events.splice(index, 1); persist(); renderTracks(); }; item.append(remove); $('events').append(item);
  });
}
function updateFrame() {
  const time = mediaReady ? clock() : 0;
  $('clock').value = `${time.toFixed(3)} s`;
  $('clock').dataset.presented=String(presented!=null&&!video.seeking);
  if (document.activeElement !== $('seek')) $('seek').value = time.toFixed(3);
  if (!canvas.width || !canvas.height) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const play = selected();
  renderProposals(time);
  for (const track of play?.dense_tracks ?? []) {
    const point = track.points.find((item) => Math.abs(item.timestamp_seconds - time) <= .000001);
    if (!point) continue;
    const [x1, y1, x2, y2] = point.bbox;
    ctx.strokeStyle = track.target_id === $('target').value ? '#ffd15b' : '#6fd2ff'; ctx.lineWidth = 3;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1); ctx.font = `${Math.max(15, canvas.width / 80)}px system-ui`; ctx.fillStyle = ctx.strokeStyle; ctx.fillText(track.target_id, x1, Math.max(17, y1 - 5));
  }
  const annotating = !!play && !!$('target').value.trim() && mediaReady && video.paused && !video.seeking;
  $('stage').classList.toggle('annotating', annotating);
  video.controls = !annotating;
}
function replacePlay(play) { draft.plays[draft.plays.findIndex((item) => item.id === selectedId)] = play; persist(); renderTracks(); updateFrame(); }

$('catalog-file').onchange = safely(async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const value = JSON.parse(await file.text());
  if (!Array.isArray(value.sources) || !value.sources.length || value.sources.some((item) => !item.id || !item.filename || !(item.bytes > 0)) || new Set(value.sources.map((item) => item.id)).size !== value.sources.length) throw Error('Catalog must contain unique source IDs, filenames and actual byte sizes.');
  persist(); catalog = value; $('source').replaceChildren(new Option('Choose a source', ''));
  catalog.sources.forEach((item) => $('source').append(new Option(`${item.id} · ${item.filename}`, item.id)));
  source = null; draft = null; mediaReady = false; video.removeAttribute('src'); video.load(); render();
  status(`Loaded ${catalog.sources.length} actual source files. Select a source, reviewer and original film.`);
});
$('source').onchange = safely(async () => {
  if(formDirty && !confirm('Save play boundaries before changing film, or discard the unsaved boundary/concept fields?')) { $('source').value=source?.id??'';return; }
  formDirty=false;
  persist(); source = catalog.sources.find((item) => item.id === $('source').value); mediaReady = false; presented = null; frameIndex = null; $('frame-index').value = '';
  $('frame-detail').textContent = 'Without a frame index, arrow controls step nominal intervals and can repeat or skip frames in variable-rate film. A dense assignment requires the actual frame index.';
  if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = null; video.removeAttribute('src'); video.load(); $('media-file').value = '';
  if (!source) { draft = null; render(); return; }
  const active=source;
  proposals=null; sourceForm(); await loadPass();
  if(source!==active)return;
  if(connection && session.catalog.sources.some(s=>s.id===source?.id)) {
    status(`Opening ${source.filename} from this package. Full-source verification can take a moment…`); video.src=`/api/media?${new URLSearchParams({source:source.id})}`; video.load();
    const response=await fetch(`/api/proposals?${new URLSearchParams({source:source.id})}`,{cache:'no-store'});
    if(response.ok && source===active) { proposals=detectionProposals(await response.json(),source);$('proposal-status').textContent=`${proposals.frames.length} actual source frames of experimental hints. No boxes are accepted automatically.`;updateFrame(); }
  }
});
$('frame-index').onchange = safely(async (event) => {
  if (!source) throw Error('Select the index source first.'); const file = event.target.files[0]; if (!file) return;
  const value = JSON.parse(await file.text());
  if (value.evidence_kind !== 'source_frame_index' || value.source_id !== source.id || value.source_clock_origin_seconds !== 0 || !/^[a-f0-9]{64}$/.test(value.media_sha256 ?? '') || value.width !== source.width || value.height !== source.height || !Array.isArray(value.frame_pts_seconds) || value.frame_pts_seconds.length < 2 || value.frame_pts_seconds.some((time, index, times) => typeof time !== 'number' || !Number.isFinite(time) || time < 0 || time > source.duration_seconds + .001 || (index > 0 && time <= times[index - 1]))) throw Error('Frame index must identify this exact zero-origin source, image dimensions and increasing actual PTS values.');
  if (source.media_sha256 && source.media_sha256 !== value.media_sha256) throw Error('Frame index media hash differs from this source.');
  frameIndex = value;
  draft.frame_index = { filename: file.name, media_sha256: value.media_sha256, start_seconds: value.start_seconds, end_seconds: value.end_seconds, actual_frames: value.frame_pts_seconds.length };
  persist(); $('frame-detail').textContent = `Actual source index loaded: ${value.frame_pts_seconds.length} native frames from ${value.start_seconds}–${value.end_seconds} s. Arrow controls visit these timestamps; annotation still requires each actual presented frame.`;
});
$('load-native-frames').onclick=safely(async()=>{
  requireMedia();if(!connection)throw Error('Start the local server for one-click exact frame steps, or import an actual frame index above.');
  const active=source, center=clock(), start=Math.max(0,center-2), end=Math.min(source.duration_seconds,center+2);
  $('load-native-frames').disabled=true;$('frame-detail').textContent='Reading actual native frame timestamps and verifying original bytes…';
  try{
    const response=await fetch(`/api/frames?${new URLSearchParams({source:source.id,start:String(start),end:String(end)})}`),value=await response.json();
    if(source!==active)return;if(!response.ok)throw Error(value.detail||'Actual frame indexing failed; check ffprobe and the original clock.');
    if(value.source_id!==source.id || (source.media_sha256 && value.media_sha256!==source.media_sha256))throw Error('Frame index source changed.');
    frameIndex=value;draft.frame_index={media_sha256:value.media_sha256,start_seconds:value.start_seconds,end_seconds:value.end_seconds,actual_frames:value.frame_pts_seconds.length};persist();
    $('frame-detail').textContent=`Exact steps ready: ${value.frame_pts_seconds.length} actual frames in ${start.toFixed(3)}–${end.toFixed(3)} source seconds. Load another window when you reach its boundary.`;
  }catch(cause){$('frame-detail').textContent='Exact frame steps unavailable. Check the error below, or import a verified source frame index.';throw cause;}finally{$('load-native-frames').disabled=false;}
});
for (const id of ['reviewer', 'pass']) $(id).onchange = safely(loadPass);
$('media-file').onchange = safely(async (event) => {
  if (!source) throw Error('Choose the corresponding source from the catalog first.');
  const file = event.target.files[0]; if (!file) return;
  if (filename(file.name) !== filename(source.filename) || file.size !== source.bytes) throw Error('Filename or byte size differs from the registered original. Choose the exact original file.');
  if(source.media_sha256) {
    if(file.size>64*1024*1024)throw Error('For this large film, use the local server and its catalog path so the full original can be verified in bounded chunks. Update the catalog path if the file moved; browser filename/size alone do not establish identity.');
    status('Checking the selected original full SHA-256…');
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');
    if(digest!==source.media_sha256)throw Error('Selected local film full SHA-256 differs from the catalog original.');
  }
  if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = URL.createObjectURL(file); mediaReady = false; presented = null;
  video.src = objectUrl; video.load();
});
$('open-local-path').onclick=safely(async()=>{
  if(!connection || !source)throw Error('Start the local server and choose the corresponding catalog source first.');
  const active=source;$('open-local-path').disabled=true;status('Verifying the moved original full SHA-256 locally. Large games may take a moment…');
  try{
    const response=await fetch('/api/source-path',{method:'PUT',headers:{'Content-Type':'application/json','X-Labeler-Token':session.token},body:JSON.stringify({source_id:source.id,path:$('local-source-path').value})}),value=await response.json();
    if(!response.ok)throw Error(value.detail||'Could not verify the moved original.');if(source!==active)return;
    source.path=value.path;presented=null;mediaReady=false;video.src=`/api/media?${new URLSearchParams({source:source.id})}&reload=${Date.now()}`;video.load();status('Exact original verified and opened. Export updated catalog to retain this moved path for another session.');
  }finally{$('open-local-path').disabled=false;}
});
video.addEventListener('loadedmetadata', safely(() => {
  if (source.duration_seconds && Math.abs(video.duration - source.duration_seconds) > Math.max(.5, source.duration_seconds * .01)) throw Error('Duration differs from the source catalog. Verify the original.');
  if((source.width && video.videoWidth!==source.width)||(source.height && video.videoHeight!==source.height)) throw Error('Actual film dimensions differ from the catalog. Do not label a substituted source.');
  canvas.width = video.videoWidth; canvas.height = video.videoHeight; mediaReady = true;
  $('stage').style.minHeight = '0';
  $('media-detail').textContent = `${source.filename} · ${video.videoWidth}×${video.videoHeight} · ${video.duration.toFixed(3)} s · ${fps().toFixed(3)} nominal fps. Full media hash is verified by the streaming freeze tool.`;
  updateFrame();
}));
video.addEventListener('error', () => { mediaReady = false; error('Film could not be opened. Check that the catalog path exists, size/hash matches, and the browser supports this original. Choose the matching local film above if it moved; never substitute another recording.'); });
for (const event of ['timeupdate', 'seeked', 'play', 'pause', 'loadeddata']) video.addEventListener(event, updateFrame);
video.addEventListener('seeking', () => { if(requestedSeek==null)presented=null;drag=null;updateFrame(); });
video.addEventListener('seeked',()=>{requestedSeek=null;updateFrame();});
if (video.requestVideoFrameCallback) {
  const frame = (_, metadata) => { presented = metadata.mediaTime; updateFrame(); video.requestVideoFrameCallback(frame); };
  video.requestVideoFrameCallback(frame);
}
for (const id of ['category', 'group', 'confirmed']) $(id).onchange = safely(() => {
  if (!source) return;
  if (id !== 'confirmed') $('confirmed').checked = false;
  const confirmed = $('confirmed').checked;
  if (confirmed && (!CATEGORIES.includes($('category').value) || !$('group').value.trim())) { $('confirmed').checked = false; throw Error('A confirmed source requires a category and independent session/game group ID.'); }
  Object.assign(source, { category: $('category').value || null, group_id: $('group').value.trim(), category_status: confirmed ? 'confirmed' : 'proposed', group_status: confirmed ? 'confirmed' : 'proposed', source_context_confirmed: confirmed });
  Object.assign(draft, { source_category: source.category, source_group_id: source.group_id, source_context_confirmed: confirmed }); persist();
});
$('export-catalog').onclick = safely(() => {
  if (!catalog.sources.length) throw Error('Open the prepared catalog first.');
  const exported=structuredClone(catalog), current=exported.sources.find(s=>s.id===source?.id);
  if(current && draft)Object.assign(current,{category:draft.source_category,group_id:draft.source_group_id,category_status:draft.source_context_confirmed?'confirmed':'proposed',group_status:draft.source_context_confirmed?'confirmed':'proposed',source_context_confirmed:draft.source_context_confirmed});
  download('sources-confirmed.json',exported);
});
$('export-draft').onclick = safely(() => {
  if (!draft) throw Error('Select a source first.');
  const errors = validateDraft(draft, duration(), source.width ?? Infinity, source.height ?? Infinity); if (errors.length) throw Error(errors.join('\n'));
  persist(); download(`${source.id}.${draft.review_pass}.${draft.reviewer_id.replace(/[^a-zA-Z0-9_-]/g, '_')}.draft.json`, draft);
  status('Draft exported. It remains unreviewed until the separate staff freeze/adjudication step.');
});
$('draft-file').onchange = safely(async (event) => {
  if (!source) throw Error('Select the matching source first.'); const file = event.target.files[0]; if (!file) return;
  const incoming = normalizeDraft(JSON.parse(await file.text()), source);
  if (incoming.source_category !== (source.category ?? null) || incoming.source_group_id !== (source.group_id ?? '') || (incoming.source_context_confirmed && !(source.group_status === 'confirmed' && source.category_status === 'confirmed'))) throw Error('Load the matching confirmed source catalog before importing this draft; source context must agree.');
  if (!PASSES.includes(incoming.review_pass) || typeof incoming.reviewer_id !== 'string' || !incoming.reviewer_id.trim()) throw Error('Imported draft requires its real reviewer ID and review pass.');
  const issues = validateDraft(incoming, duration(), source.width ?? Infinity, source.height ?? Infinity); if (issues.length) throw Error(issues.join('\n'));
  if(connection) {
    const existing=await connection.read(source.id,incoming.reviewer_id,incoming.review_pass);
    if(existing?.plays.length && !confirm('Replace this reviewer/pass with the imported draft? Export the existing draft first to keep a portable copy. Local revision history is retained.'))return;
  }
  persist(); draft = incoming; $('reviewer').value = draft.reviewer_id; $('pass').value = draft.review_pass; selectedId = null; persist(); newPlay(); render(); status('Imported as an unreviewed draft; independent review flags were reset.');
});
$('comparison-file').onchange = safely(async (event) => {
  if (!draft || !$('reviewer').value.trim()) throw Error('Select your draft and reviewer ID first.');
  const file = event.target.files[0]; if (!file) return;
  if ($('pass').value !== 'adjudication') throw Error('Primary and secondary passes must remain independent. Open comparisons only in a separate adjudication pass.');
  const other = normalizeDraft(JSON.parse(await file.text()), source);
  $('comparison').textContent = JSON.stringify(compareDrafts(draft, other), null, 2);
});
$('new-play').onclick = safely(() => { requireMedia(); if(formDirty&&!confirm('Discard the unsaved boundary/concept fields and start another play?'))return;newPlay(); });
$('save-play').onclick = safely(() => {
  requireEdit();
  const previous = selected();
  const play = { ...structuredClone(previous ?? { dense_tracks: [], identities: [], events: [] }), id: $('play-id').value.trim(), canonical_play_id: $('canonical-id').value.trim(), start_seconds: $('start').value === '' ? null : Number($('start').value), snap_seconds: $('snap').value === '' ? null : Number($('snap').value), end_seconds: $('end').value === '' ? null : Number($('end').value), notes: $('notes').value, practice_period:$('practice-period').value||null, offense_concept:$('offense-concept').value.trim()||null, defense_concept:$('defense-concept').value.trim()||null, double_labeled: false, adjudicated: false };
  const issues = validatePlay(play, duration(), canvas.width, canvas.height); if (issues.length) throw Error(issues.join('\n'));
  if (draft.plays.some((item) => item.id === play.id && item.id !== selectedId)) throw Error('Play ID already exists. Select that play to edit it.');
  if (previous) draft.plays[draft.plays.indexOf(previous)] = play; else draft.plays.push(play);
  draft.plays.sort((a, b) => a.start_seconds - b.start_seconds); selectedId = play.id; formDirty=false;$('form-state').textContent='Play boundaries and concept notes saved in this draft.';persist(); render(); status(`Saved ${play.id} as a draft. Canonical IDs must be reconciled across all related views.`);
});
$('delete-play').onclick = safely(() => { requirePlay(); draft.plays = draft.plays.filter((play) => play.id !== selectedId); persist(); newPlay(); render(); });
$('plays').onchange = () => {if(formDirty&&!confirm('Discard unsaved boundary/concept fields and select this saved play?')){$('plays').value=selectedId??'';return;}formDirty=false;$('form-state').textContent='';choosePlay($('plays').value);};
function markFormDirty(){formDirty=true;$('form-state').textContent='Boundary/concept changes are not saved. Choose Save play boundaries.';}
for(const id of ['play-id','canonical-id','start','snap','end','notes','practice-period','offense-concept','defense-concept']) $(id).addEventListener('input',markFormDirty);
function capture(key) { requireEdit(); $(key).value = clock().toFixed(6);markFormDirty(); }
for (const key of ['start', 'snap', 'end']) $(`capture-${key}`).onclick = safely(() => capture(key));
async function togglePlay() { requireMedia(); if (video.paused) await video.play(); else video.pause(); }
function step(direction) {
  requireMedia(); video.pause();
  const current = clock();
  const target = frameIndex ? direction > 0 ? frameIndex.frame_pts_seconds.find((value) => value > current + .000001) : frameIndex.frame_pts_seconds.findLast((value) => value < current - .000001) : Math.max(0, Math.min(video.duration, current + direction / fps()));
  if (target == null) throw Error('Reached the boundary of this dense assignment frame index.');
  seekTo(Math.min(video.duration, target + .000001));
}
$('toggle-play').onclick = safely(togglePlay); $('back-frame').onclick = safely(() => step(-1)); $('next-frame').onclick = safely(() => step(1));
$('seek-button').onclick = safely(() => { requireMedia(); const value = Number($('seek').value); if (!Number.isFinite(value) || value < 0 || value > duration()) throw Error('Seek inside the original film.'); video.pause(); seekTo(value); });
for(const [id,delta] of [['back-second',-1],['next-second',1]]) $(id).onclick=safely(()=>{requireMedia();video.pause();seekTo(Math.min(video.duration,Math.max(0,video.currentTime+delta)));});
$('speed').onchange=()=>{video.playbackRate=Number($('speed').value);};
document.addEventListener('keydown', safely(async (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(document.activeElement.tagName) || !mediaReady) return;
  if (event.code === 'Space') { event.preventDefault(); await togglePlay(); }
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); step(event.key === 'ArrowLeft' ? -1 : 1); }
  else if ({ s: 'start', n: 'snap', e: 'end' }[event.key.toLowerCase()]) capture({ s: 'start', n: 'snap', e: 'end' }[event.key.toLowerCase()]);
}));
$('target').oninput = updateFrame;
const pixel = (event) => { const rect = canvas.getBoundingClientRect(); return [Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / rect.width)), Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / rect.height))]; };
canvas.onpointerdown = safely((event) => { requirePlay(); if (!video.paused) throw Error('Pause on the source frame before drawing.'); if ($('visibility').value !== 'visible') throw Error('Record occlusion/offscreen state without inventing a box.'); drag = pixel(event); canvas.setPointerCapture(event.pointerId); });
canvas.onpointermove = (event) => { if (!drag) return; updateFrame(); const point = pixel(event); ctx.strokeStyle = '#ffd15b'; ctx.lineWidth = 3; ctx.strokeRect(drag[0], drag[1], point[0] - drag[0], point[1] - drag[1]); };
canvas.onpointerup = safely((event) => {
  if (!drag) return; const begin = drag; drag = null; const end = pixel(event), play = requirePlay();
  const point = { timestamp_seconds: clock(), bbox: [Math.min(begin[0], end[0]), Math.min(begin[1], end[1]), Math.max(begin[0], end[0]), Math.max(begin[1], end[1])].map((value) => Math.round(value * 1000) / 1000), visibility_state: 'visible', shot_id: $('shot').value.trim() };
  if (!point.shot_id) throw Error('Enter the actual shot/view ID; change it at camera cuts.');
  const result=putPoint(play, $('target').value.trim(), point, canvas.width, canvas.height); applyActor(result); replacePlay(result); status(`Recorded actual ${point.timestamp_seconds.toFixed(6)} s box for ${$('target').value.trim()}.`);
});
canvas.onpointercancel = () => { drag = null; updateFrame(); };
$('record-visibility').onclick = safely(() => {
  const play = structuredClone(requirePlay()), id = $('target').value.trim();
  if (!id) throw Error('Choose a target ID first.');
  if ($('visibility').value === 'visible') throw Error('Record an actual visible observation by drawing its box. Visibility-only records are for occluded or offscreen targets.');
  const time = clock(); if (time < play.start_seconds || time > play.end_seconds) throw Error('Visibility state must be inside the selected play.');
  const track = play.dense_tracks.find((item) => item.target_id === id); if (!track) throw Error('Create the target with a real visible box first.');
  track.visibility_events ??= []; track.visibility_events.push({ timestamp_seconds: time, visibility_state: $('visibility').value, shot_id: $('shot').value.trim() });
  track.visibility_events.sort((a, b) => a.timestamp_seconds - b.timestamp_seconds); replacePlay(play);
});
$('remove-point').onclick = safely(() => {
  const play = structuredClone(requirePlay()), track = play.dense_tracks.find((item) => item.target_id === $('target').value.trim());
  if (!track?.points.length) throw Error('Select a target with recorded points.');
  const index = track.points.reduce((best, point, i) => Math.abs(point.timestamp_seconds - clock()) < Math.abs(track.points[best].timestamp_seconds - clock()) ? i : best, 0);
  if (Math.abs(track.points[index].timestamp_seconds - clock()) > .000001) throw Error('Seek to the exact labeled source frame before removing its observation.');
  track.points.splice(index, 1); replacePlay(play);
});
$('delete-track').onclick = safely(() => {
  const play = structuredClone(requirePlay()), id = $('target').value.trim();
  if (!play.dense_tracks.some((item) => item.target_id === id)) throw Error('Select an existing draft target.');
  play.dense_tracks = play.dense_tracks.filter((item) => item.target_id !== id);
  play.identities = (play.identities ?? []).filter((item) => item.target_id !== id);
  replacePlay(play);
});
$('save-identity').onclick = safely(() => {
  const play = structuredClone(requirePlay()), id = $('target').value.trim();
  applyActor(play);
  const identity = { target_id: id, canonical_entity_id: $('entity-id').value.trim() || null, player_id: $('player-id').value.trim() || null, readable: $('readable').checked, team_resolved: $('team-resolved').checked };
  play.identities = [...(play.identities ?? []).filter((item) => item.target_id !== id), identity];
  const issues = validatePlay(play, duration()); if (issues.length) throw Error(issues.join('\n')); replacePlay(play);
});
$('add-event').onclick = safely(() => {
  const play = structuredClone(requirePlay()); play.events ??= [];
  play.events.push({ event_type: $('event-type').value, timestamp_seconds: clock() });
  const issues = validatePlay(play, duration()); if (issues.length) throw Error(issues.join('\n'));
  play.events.sort((a, b) => a.timestamp_seconds - b.timestamp_seconds); replacePlay(play);
});
window.addEventListener('beforeunload', event=>{persist();if(formDirty){event.preventDefault();event.returnValue='';}});

function applyActor(play) {
  const target=play.dense_tracks.find(t=>t.target_id===$('target').value.trim());
  if(!target) throw Error('Draw or accept a real visible box for this target before saving actor evidence.');
  const kind=$('actor-kind').value, side=$('actor-side').value;
  if(kind==='referee' && !['unknown','not_applicable'].includes(side)) throw Error('Referees have no offense/defense side. Choose Not applicable or Unknown.');
  target.actor_kind=kind; target.side=side;
}
function installCatalog(value) {
  if(!Array.isArray(value.sources)||!value.sources.length) throw Error('Catalog has no sources.');
  catalog=value; $('source').replaceChildren(new Option('Choose a source',''));
  value.sources.forEach(s=>$('source').append(new Option(s.filename,s.id)));
}
$('save-draft').onclick=safely(async()=>{
  if(!draft || !$('reviewer').value.trim()) throw Error('Select your source and enter your reviewer ID first.');
  const issues=validateDraft(draft,duration(),source.width,source.height);if(issues.length) throw Error(issues.join('\n'));
  if(!connection) throw Error('Local folder saving needs server.py. Export draft JSON now to keep a portable copy.');
  persist(); await connection.states.get(connection.key(draft.source_id,draft.reviewer_id,draft.review_pass)).queue;
});
$('reload-draft').onclick=safely(async()=>{ if(!connection) throw Error('Start the local draft server to reload saved files.'); if(!confirm('Reload the saved reviewer/pass? Export a recovery copy first if you have unsaved changes.')) return; await loadPass(); });
function renderProposals(time) {
  const frame=proposals?.frames.find(f=>Math.abs(f.timestamp_seconds-time)<=.000001);
  const choice=$('proposal-choice'), previous=choice.value;
  choice.replaceChildren(new Option(frame?.detections.length?'Choose an unreviewed hint':'No hints at this exact frame',''));
  for(const d of frame?.detections??[]) { choice.append(new Option(`${d.proposal_id} · suggested ${d.actor_kind} · UNREVIEWED`,d.proposal_id)); ctx.strokeStyle='#d18bff';ctx.lineWidth=2;ctx.setLineDash([8,6]);ctx.strokeRect(d.bbox[0],d.bbox[1],d.bbox[2]-d.bbox[0],d.bbox[3]-d.bbox[1]);ctx.setLineDash([]); }
  choice.value=previous;
}
$('proposal-file').onchange=safely(async event=>{if(!source) throw Error('Select the proposal source first.'); const file=event.target.files[0];if(!file)return;proposals=detectionProposals(JSON.parse(await file.text()),source);$('proposal-status').textContent=`${proposals.frames.length} actual source frames of experimental hints. No hints have been accepted automatically.`;updateFrame();});
for(const [id,direction] of [['previous-proposal',-1],['next-proposal',1]]) $(id).onclick=safely(()=>{requireMedia();if(!proposals)throw Error('Load source-bound proposal hints first.');const time=clock(),frame=direction>0?proposals.frames.find(f=>f.timestamp_seconds>time+.000001):proposals.frames.findLast(f=>f.timestamp_seconds<time-.000001);if(!frame)throw Error('No further hinted frame in this source.');video.pause();seekTo(frame.timestamp_seconds);});
$('accept-proposal').onclick=safely(()=>{
  const play=requirePlay(),frame=proposals?.frames.find(f=>Math.abs(f.timestamp_seconds-clock())<=.000001),hint=frame?.detections.find(d=>d.proposal_id===$('proposal-choice').value);
  if(!video.paused||!hint)throw Error('Pause on the exact hinted source frame and select a hint.');
  if(!$('shot').value.trim())throw Error('Enter the observed shot/view ID before accepting a hint.');
  const point={timestamp_seconds:clock(),bbox:hint.bbox,visibility_state:'visible',shot_id:$('shot').value.trim(),proposal_id:hint.proposal_id,proposal_provenance:structuredClone(proposals.provenance??{}),annotation_origin:'staff_accepted_experimental_hint'};
  const result=putPoint(play,$('target').value.trim(),point,canvas.width,canvas.height);applyActor(result);replacePlay(result);
  status('Accepted one box into your unreviewed draft with your selected actor/side. Other hints remain unaccepted.');
});
async function boot() {
  try {
    const response=await fetch('/api/session',{cache:'no-store'});
    if(!response.ok) throw Error('static');
    session=await response.json();connection=new DraftConnection(session);installCatalog(session.catalog);
    $('connection-status').textContent=`Connected to local draft folder: ${session.draft_store}. Originals are read only; each save keeps revision history.`;
    status('Enter your reviewer ID and choose a film. The local server opens its catalog original automatically.');
    try {
      const previous=JSON.parse(localStorage.getItem('footiq-labeler-workspace')||'null');
      if(previous && catalog.sources.some(s=>s.id===previous.source)) { $('reviewer').value=previous.reviewer??'';$('pass').value=PASSES.includes(previous.pass)?previous.pass:'primary';$('source').value=previous.source;await $('source').onchange(); }
    }catch{status('Choose your reviewer and source to reopen the saved draft.');}
  } catch {
    $('connection-status').textContent='Browser-only mode: no local save server. Start server.py or the supplied launcher for durable saves and automatic film selection. You can still open a catalog/film and export draft JSON.';
    $('save-state').textContent='Folder saving unavailable · use draft exports';
  }
}
void boot();
