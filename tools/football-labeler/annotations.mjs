export const finite = (value) => typeof value === 'number' && Number.isFinite(value);
export const PASSES = ['primary', 'secondary', 'adjudication'];
export const CATEGORIES = ['practice', 'coach_game', 'television'];

export function createDraft(source, reviewerId = '', pass = 'primary') {
  return {
    schema_version: 1, evidence_kind: 'annotation_draft', reviewed: false,
    source_id: source.id, media_sha256: source.media_sha256 ?? null,
    source_category: source.category ?? null, source_group_id: source.group_id ?? '',
    source_context_confirmed: source.group_status === 'confirmed' && source.category_status === 'confirmed',
    reviewer_id: reviewerId, review_pass: PASSES.includes(pass) ? pass : 'primary', plays: [],
  };
}

export function normalizeDraft(value, source) {
  if (!value || value.source_id !== source.id || !Array.isArray(value.plays)) throw Error('Draft source does not match the selected film.');
  if (value.media_sha256 && source.media_sha256 && value.media_sha256 !== source.media_sha256) throw Error('Media hashes differ. Verify the source before importing labels.');
  const draft = { ...createDraft(source), ...structuredClone(value), evidence_kind: 'annotation_draft', reviewed: false };
  draft.plays.forEach((play) => { play.double_labeled = false; play.adjudicated = false; });
  return draft;
}

export function validatePlay(play, duration = Infinity, width = Infinity, height = Infinity) {
  const errors = [];
  if (!play.id || !play.canonical_play_id) errors.push('Play ID and canonical play ID are required.');
  if (!finite(play.start_seconds) || !finite(play.end_seconds) || play.start_seconds < 0 || play.end_seconds <= play.start_seconds || play.end_seconds > duration + .001) errors.push('Start and end must form a positive interval inside the original source.');
  if (play.snap_seconds != null && (!finite(play.snap_seconds) || play.snap_seconds < play.start_seconds || play.snap_seconds >= play.end_seconds)) errors.push('Snap must be before the play end and inside the play, or left unknown.');
  for (const event of play.events ?? []) if (!event.event_type || !finite(event.timestamp_seconds) || event.timestamp_seconds < play.start_seconds || event.timestamp_seconds > play.end_seconds) errors.push('Each event needs a name and source time inside the play.');
  const targets = new Set();
  for (const track of play.dense_tracks ?? []) {
    if (!track.target_id || targets.has(track.target_id)) errors.push('Track target IDs must be nonempty and unique within a play.');
    targets.add(track.target_id);
    if(!['player','referee','unknown'].includes(track.actor_kind??'unknown') || !['offense','defense','unknown','not_applicable'].includes(track.side??'unknown')) errors.push('Actor kind and observed side must be explicit supported values or unknown.');
    if(track.actor_kind==='referee' && !['unknown','not_applicable'].includes(track.side??'unknown')) errors.push('A referee cannot have offense/defense responsibility.');
    let last = -Infinity;
    for (const point of track.points ?? []) {
      const b = point.bbox;
      if (!finite(point.timestamp_seconds) || point.timestamp_seconds <= last || point.timestamp_seconds < play.start_seconds || point.timestamp_seconds > play.end_seconds) errors.push('Track points must have increasing source timestamps inside the play.');
      if (!Array.isArray(b) || b.length !== 4 || !b.every(finite) || b[0] < 0 || b[1] < 0 || b[2] > width || b[3] > height || b[2] <= b[0] || b[3] <= b[1]) errors.push('A visible box must use real pixel bounds [left, top, right, bottom] inside the source image.');
      if ((point.field_x != null && !finite(point.field_x)) || (point.field_y != null && !finite(point.field_y))) errors.push('Field coordinates must be measured numbers or unknown.');
      last = point.timestamp_seconds;
    }
  }
  for (const identity of play.identities ?? []) {
    if (!targets.has(identity.target_id)) errors.push('Identity target must match an existing manually labeled track.');
    if ((identity.player_id || identity.canonical_entity_id) && (!identity.readable || !identity.team_resolved)) errors.push('Known identities require readable jersey and resolved team evidence. Leave unknown identities blank.');
    if (identity.player_id && !identity.canonical_entity_id) errors.push('A known roster identity needs a canonical entity ID for cross-view deduplication.');
    if((identity.player_id || identity.canonical_entity_id) && play.dense_tracks?.find(t=>t.target_id===identity.target_id)?.actor_kind==='referee') errors.push('A referee cannot receive a player roster identity.');
  }
  return [...new Set(errors)];
}

export function validateDraft(draft, duration = Infinity, width = Infinity, height = Infinity) {
  const errors = [];
  if (typeof draft.reviewer_id !== 'string' || !draft.reviewer_id.trim()) errors.push('Enter a reviewer ID before exporting.');
  if (!PASSES.includes(draft.review_pass)) errors.push('Choose primary, secondary or adjudication pass.');
  if (draft.reviewed !== false || draft.evidence_kind !== 'annotation_draft') errors.push('The labeling tool exports drafts only. Use the separate human review/freeze step.');
  const ids = new Set();
  for (const play of draft.plays ?? []) {
    if (ids.has(play.id)) errors.push(`Duplicate play ID: ${play.id}`);
    ids.add(play.id);
    errors.push(...validatePlay(play, duration, width, height).map((message) => `${play.id}: ${message}`));
  }
  return [...new Set(errors)];
}

export function putPoint(play, targetId, point, width, height) {
  const b = point.bbox;
  if (!targetId?.trim() || !Array.isArray(b) || b.length !== 4 || !b.every(finite) || !finite(point.timestamp_seconds)
    || b[0] < 0 || b[1] < 0 || b[2] > width || b[3] > height || b[2] <= b[0] || b[3] <= b[1]
    || point.timestamp_seconds < play.start_seconds || point.timestamp_seconds > play.end_seconds) throw Error('Choose a target and draw its actual visible box inside the play.');
  const result = structuredClone(play);
  result.dense_tracks ??= [];
  let track = result.dense_tracks.find((item) => item.target_id === targetId);
  if (!track) { track = { target_id: targetId, points: [], visibility_events: [] }; result.dense_tracks.push(track); }
  track.points = [...track.points.filter((item) => Math.abs(item.timestamp_seconds - point.timestamp_seconds) > .000001), structuredClone(point)].sort((a, z) => a.timestamp_seconds - z.timestamp_seconds);
  return result;
}

export function compareDrafts(left, right) {
  if (left.source_id !== right.source_id) throw Error('Comparison requires the same source.');
  if (!left.reviewer_id || !right.reviewer_id || left.reviewer_id === right.reviewer_id) throw Error('Independent comparison requires two different reviewer IDs.');
  if (left.media_sha256 && right.media_sha256 && left.media_sha256 !== right.media_sha256) throw Error('Comparison media hashes differ.');
  const keys = new Set([...left.plays, ...right.plays].map((play) => play.canonical_play_id));
  return [...keys].map((key) => {
    const a = left.plays.filter((play) => play.canonical_play_id === key).sort((x, y) => x.start_seconds - y.start_seconds);
    const b = right.plays.filter((play) => play.canonical_play_id === key).sort((x, y) => x.start_seconds - y.start_seconds);
    const windows = (plays) => plays.map((play) => ({ id: play.id, boundaries: [play.start_seconds, play.snap_seconds, play.end_seconds] }));
    const bounds = (plays) => plays.map((play) => [play.start_seconds, play.snap_seconds, play.end_seconds]);
    return { canonical_play_id: key, primary: windows(a), comparison: windows(b),
      matching_boundaries: a.length > 0 && a.length === b.length && JSON.stringify(bounds(a)) === JSON.stringify(bounds(b)),
      identity_disagreement: JSON.stringify(a.map((play) => play.identities ?? [])) !== JSON.stringify(b.map((play) => play.identities ?? [])),
      note: 'Manual adjudication required; matching timestamps do not establish correct labels.' };
  });
}
