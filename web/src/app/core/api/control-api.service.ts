import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { AppConfigService } from '../config/app-config.service';
import {
  AudioCutRequest,
  EnqueueRequest,
  ErrorLogResponse,
  FolderScanRequest,
  Job,
  MprisPlayer,
  PlaybackState,
  PlaySession,
  PlaySessionsResponse,
  PlayersResponse,
  QueueItem,
  QueueResponse,
  QueueReorderRequest,
  RecordDevicesResponse,
  RecordStatusResponse,
  SampleRequest,
  WindowRequest,
  WorkerStatus
} from '../models';

export interface PlayRequest {
  url?: string;
  kind?: string;
  artist?: string;
  title?: string;
  prefer_audio?: boolean;
}

/** Client for the host-only Control API (:8765) — playback, recording, queue, workers. */
@Injectable({ providedIn: 'root' })
export class ControlApiService {
  constructor(
    private http: HttpClient,
    private config: AppConfigService
  ) {}

  private get base(): string {
    return this.config.controlApiUrl;
  }

  health(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${this.base}/api/health`);
  }

  // --- Playback launch/sessions ---

  play(request: PlayRequest): Observable<PlaySession> {
    return this.http.post<PlaySession>(`${this.base}/api/play`, request);
  }

  listPlaySessions(): Observable<PlaySessionsResponse> {
    return this.http.get<PlaySessionsResponse>(`${this.base}/api/play/sessions`);
  }

  getPlaySession(sessionId: string): Observable<PlaySession> {
    return this.http.get<PlaySession>(`${this.base}/api/play/sessions/${sessionId}`);
  }

  stopPlaySession(sessionId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/play/sessions/${sessionId}`);
  }

  // --- MPRIS players ---

  listPlayers(): Observable<PlayersResponse> {
    return this.http.get<PlayersResponse>(`${this.base}/api/players`);
  }

  getCurrentPlayer(player?: string): Observable<MprisPlayer> {
    return this.http.get<MprisPlayer>(`${this.base}/api/players/current`, {
      params: player ? { player } : {}
    });
  }

  playPause(player?: string): Observable<void> {
    return this.http.post<void>(`${this.base}/api/players/play-pause`, { player });
  }

  pause(player?: string): Observable<void> {
    return this.http.post<void>(`${this.base}/api/players/pause`, { player });
  }

  next(player?: string): Observable<void> {
    return this.http.post<void>(`${this.base}/api/players/next`, { player });
  }

  previous(player?: string): Observable<void> {
    return this.http.post<void>(`${this.base}/api/players/previous`, { player });
  }

  seek(offsetS: number, player?: string): Observable<void> {
    return this.http.post<void>(`${this.base}/api/players/seek`, { offset_s: offsetS, player });
  }

  // --- Recording ---

  recordingDevices(): Observable<RecordDevicesResponse> {
    return this.http.get<RecordDevicesResponse>(`${this.base}/api/record/devices`);
  }

  startRecording(source?: string, keepAudio = false, note?: string): Observable<unknown> {
    return this.http.post(`${this.base}/api/record/start`, {
      source,
      keep_audio: keepAudio,
      note
    });
  }

  stopRecording(recordingId?: number): Observable<unknown> {
    return this.http.post(`${this.base}/api/record/stop`, { recording_id: recordingId });
  }

  recordStatus(): Observable<RecordStatusResponse> {
    return this.http.get<RecordStatusResponse>(`${this.base}/api/record/status`);
  }

  analyseRecording(recordingId: number, keep = false): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/recordings/${recordingId}/analyse`, null, {
      params: { keep }
    });
  }

  deleteRecordingAudio(recordingId: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/recordings/${recordingId}/audio`);
  }

  recordingTrackAudioUrl(recordingId: number, trackIndex: number): string {
    return `${this.base}/api/recordings/${recordingId}/tracks/${trackIndex}/audio`;
  }

  recordingTrackPageUrl(recordingId: number, trackIndex: number): string {
    return `${this.base}/recordings/${recordingId}/tracks/${trackIndex}`;
  }

  importRadioSession(
    sessionId: string,
    saveAudio = true,
    resolveStreaming = true
  ): Observable<unknown> {
    return this.http.post(`${this.base}/api/radio/sessions/${sessionId}/import`, null, {
      params: { save_audio: saveAudio, resolve_streaming: resolveStreaming }
    });
  }

  // --- Queue (Phase 0 addition) ---

  getQueue(): Observable<QueueResponse> {
    return this.http.get<QueueResponse>(`${this.base}/api/queue`);
  }

  enqueue(request: EnqueueRequest): Observable<QueueItem> {
    return this.http.post<QueueItem>(`${this.base}/api/queue`, request);
  }

  removeFromQueue(index: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/queue/${index}`);
  }

  reorderQueue(request: QueueReorderRequest): Observable<QueueResponse> {
    return this.http.patch<QueueResponse>(`${this.base}/api/queue/reorder`, request);
  }

  // --- Jobs (Phase 0 addition) ---

  getJob(jobId: string): Observable<Job> {
    return this.http.get<Job>(`${this.base}/api/jobs/${jobId}`);
  }

  // --- Workers / logs / scan ---

  workersStatus(): Observable<WorkerStatus> {
    return this.http.get<WorkerStatus>(`${this.base}/api/workers/status`);
  }

  scaleWorkers(target: number): Observable<unknown> {
    return this.http.post(`${this.base}/api/workers/scale`, { target });
  }

  recentErrors(lines = 50): Observable<ErrorLogResponse> {
    return this.http.get<ErrorLogResponse>(`${this.base}/api/logs/errors`, {
      params: { lines }
    });
  }

  recentEvents(sinceTs?: number, limit = 50): Observable<{ events: unknown[]; count: number }> {
    const params: Record<string, string | number> = { limit };
    if (sinceTs !== undefined) params['since_ts'] = sinceTs;
    return this.http.get<{ events: unknown[]; count: number }>(`${this.base}/api/events/recent`, {
      params
    });
  }

  scanFolder(request: FolderScanRequest): Observable<Job | Record<string, unknown>> {
    return this.http.post<Job | Record<string, unknown>>(`${this.base}/api/scan/folder`, request);
  }

  cutAudio(request: AudioCutRequest): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/api/audio/cut`, request);
  }

  sample(request: SampleRequest): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/api/sample`, request);
  }

  getPlayerWindow(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.base}/api/players/window`);
  }

  updatePlayerWindow(request: WindowRequest): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/api/players/window`, request);
  }

  restartPlayerWindow(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base}/api/players/window/restart`, {});
  }

  // --- Stage state (single snapshot; see also StageStreamService for SSE) ---

  getStageState(): Observable<PlaybackState> {
    return this.http.get<PlaybackState>(`${this.base}/api/stage/state`);
  }
}
