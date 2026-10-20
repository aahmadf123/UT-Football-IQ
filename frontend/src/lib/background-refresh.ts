/**
 * Pause background polling while film is playing.
 *
 * Several views poll the API every few seconds (app-wide data, overlay
 * payloads). Each refresh parses JSON and replaces state, which landed as
 * main-thread work every couple of seconds during playback. The player
 * pauses refresh while a video plays and resumes it on pause or end.
 */

const pausedBy = new Set<string>();

export function pauseBackgroundRefresh(reason: string): void {
  pausedBy.add(reason);
}

export function resumeBackgroundRefresh(reason: string): void {
  pausedBy.delete(reason);
}

export function isBackgroundRefreshPaused(): boolean {
  return pausedBy.size > 0;
}
