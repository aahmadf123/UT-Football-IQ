import assert from 'node:assert/strict';
import test from 'node:test';
import { compareDrafts, createDraft, normalizeDraft, putPoint, validateDraft, validatePlay } from './annotations.mjs';

const source = { id: 'test-source', media_sha256: 'a'.repeat(64), category: 'practice', group_id: 'same-practice', group_status: 'proposed', category_status: 'proposed' };
const play = () => ({ id: 'window-1', canonical_play_id: 'canonical-1', start_seconds: 0, snap_seconds: 1, end_seconds: 4, dense_tracks: [], identities: [] });
test('draft creation and import never qualify human review or invent source confirmation', () => {
  const draft = createDraft(source, 'reviewer-a');
  assert.equal(draft.reviewed, false); assert.equal(draft.source_context_confirmed, false);
  const restored = normalizeDraft({ ...draft, evidence_kind: 'human_reviewed', reviewed: true, plays: [{ ...play(), double_labeled: true, adjudicated: true }] }, source);
  assert.equal(restored.reviewed, false); assert.equal(restored.evidence_kind, 'annotation_draft');
  assert.equal(restored.plays[0].double_labeled, false); assert.equal(restored.plays[0].adjudicated, false);
  assert.throws(() => normalizeDraft({ ...draft, media_sha256: 'b'.repeat(64) }, source), /hashes differ/);
});
test('source-bounded windows and identities preserve unknowns instead of accepting guesses', () => {
  const value = play(); value.snap_seconds = null;
  assert.deepEqual(validatePlay(value, 4), []);
  assert.ok(validatePlay({ ...value, end_seconds: 5 }, 4).length);
  value.identities = [{ target_id: 'missing', player_id: 'roster-id', readable: false, team_resolved: false }];
  assert.ok(validatePlay(value).some((message) => message.includes('Identity target')));
  assert.ok(validatePlay(value).some((message) => message.includes('readable jersey')));
});
test('manual points use actual finite image bounds and source time, retaining shot barriers', () => {
  const value = putPoint(play(), 'target-1', { timestamp_seconds: 2, bbox: [10, 20, 50, 80], visibility_state: 'visible', shot_id: 'shot-a' }, 1280, 720);
  const newer = putPoint(value, 'target-1', { timestamp_seconds: 2, bbox: [11, 21, 51, 81], visibility_state: 'visible', shot_id: 'shot-b' }, 1280, 720);
  assert.equal(newer.dense_tracks[0].points.length, 1); assert.equal(newer.dense_tracks[0].points[0].shot_id, 'shot-b');
  assert.equal(value.dense_tracks[0].points[0].bbox[0], 10);
  assert.throws(() => putPoint(value, 'target-1', { timestamp_seconds: 5, bbox: [1, 1, 20, 20] }, 1280, 720));
  assert.throws(() => putPoint(value, 'target-1', { timestamp_seconds: 2, bbox: [1, 1, 1400, 20] }, 1280, 720));
  assert.ok(validatePlay({ ...value, dense_tracks: [{ target_id: 'target-1', points: [{ timestamp_seconds: 2, bbox: [1, 1, 1400, 20] }] }] }, 4, 1280, 720).length);
});
test('review comparison retains all angle and replay windows of one canonical play', () => {
  const a = { ...createDraft(source, 'reviewer-a'), plays: [play(), { ...play(), id: 'window-2', start_seconds: 10, snap_seconds: 11, end_seconds: 14 }] };
  const b = { ...createDraft(source, 'reviewer-b', 'secondary'), plays: [play(), { ...play(), id: 'window-2', start_seconds: 10, snap_seconds: 12, end_seconds: 14 }] };
  const result = compareDrafts(a, b);
  assert.equal(result.length, 1); assert.equal(result[0].primary.length, 2); assert.equal(result[0].comparison.length, 2);
  assert.equal(result[0].matching_boundaries, false);
  assert.throws(() => compareDrafts(a, { ...b, reviewer_id: 'reviewer-a' }), /different reviewer/);
});
test('draft exports require reviewer attribution and unique source-local IDs', () => {
  const draft = { ...createDraft(source), plays: [play(), play()] };
  assert.ok(validateDraft(draft).some((message) => message.includes('reviewer ID')));
  assert.ok(validateDraft(draft).some((message) => message.includes('Duplicate play ID')));
});
