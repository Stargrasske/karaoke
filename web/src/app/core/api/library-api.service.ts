import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { AppConfigService } from '../config/app-config.service';
import {
  LibraryWorkerStatus,
  LogResponse,
  Recording,
  SoundsLikeResult,
  StatsSummary,
  Track,
  TrackDetail
} from '../models';

export interface TrackListParams {
  q?: string;
  genre?: string;
  limit?: number;
  offset?: number;
}

export interface RecordingListParams {
  status?: string;
  source?: string;
  since?: number;
  until?: number;
  has_marks?: boolean;
}

/** Client for the read-only Library API (:8000) — tracks, recordings, radio, stats. */
@Injectable({ providedIn: 'root' })
export class LibraryApiService {
  constructor(
    private http: HttpClient,
    private config: AppConfigService
  ) {}

  private get base(): string {
    return this.config.libraryApiUrl;
  }

  health(): Observable<{ status: string }> {
    return this.http.get<{ status: string }>(`${this.base}/api/health`);
  }

  listTracks(params: TrackListParams = {}): Observable<Track[]> {
    let httpParams = new HttpParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        httpParams = httpParams.set(key, String(value));
      }
    }
    return this.http.get<Track[]>(`${this.base}/api/tracks`, { params: httpParams });
  }

  getTrack(trackId: number): Observable<TrackDetail> {
    return this.http.get<TrackDetail>(`${this.base}/api/tracks/${trackId}`);
  }

  lookupLyrics(artist: string, title: string): Observable<TrackDetail> {
    return this.http.post<TrackDetail>(`${this.base}/api/lyrics/lookup`, { artist, title });
  }

  getTrackAnalysisHistory(trackId: number): Observable<unknown[]> {
    return this.http.get<unknown[]>(`${this.base}/api/tracks/${trackId}/analysis/history`);
  }

  getSoundsLike(trackId: number): Observable<SoundsLikeResult[]> {
    return this.http.get<SoundsLikeResult[]>(`${this.base}/api/tracks/${trackId}/sounds-like`);
  }

  findSource(trackId: number): Observable<unknown> {
    return this.http.post(`${this.base}/api/sources/find`, { track_id: trackId });
  }

  suggestQueue(seedTrackIds: number[]): Observable<SoundsLikeResult[]> {
    return this.http.post<SoundsLikeResult[]>(`${this.base}/api/queue/suggest`, {
      track_ids: seedTrackIds
    });
  }

  getStats(): Observable<StatsSummary> {
    return this.http.get<StatsSummary>(`${this.base}/api/stats`);
  }

  listRecordings(params: RecordingListParams = {}): Observable<Recording[]> {
    let httpParams = new HttpParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        httpParams = httpParams.set(key, String(value));
      }
    }
    return this.http.get<Recording[]>(`${this.base}/api/recordings`, { params: httpParams });
  }

  getRecording(recordingId: number): Observable<Recording> {
    return this.http.get<Recording>(`${this.base}/api/recordings/${recordingId}`);
  }

  updateRecording(
    recordingId: number,
    patch: { note?: string; keep_audio?: boolean }
  ): Observable<Recording> {
    return this.http.patch<Recording>(`${this.base}/api/recordings/${recordingId}`, patch);
  }

  recordingAudioUrl(recordingId: number, trackIndex: number): string {
    return `${this.base}/api/recordings/${recordingId}/tracks/${trackIndex}/audio`;
  }

  listRadioSessions(limit = 50): Observable<{ sessions: Array<Record<string, unknown>>; count: number }> {
    return this.http.get<{ sessions: Array<Record<string, unknown>>; count: number }>(`${this.base}/api/radio/sessions`, {
      params: { limit }
    });
  }

  getRadioSession(sessionId: string): Observable<{ session: Record<string, unknown>; tracks: unknown[]; count: number }> {
    return this.http.get<{ session: Record<string, unknown>; tracks: unknown[]; count: number }>(`${this.base}/api/radio/sessions/${sessionId}`);
  }

  getWorkers(): Observable<LibraryWorkerStatus> {
    return this.http.get<LibraryWorkerStatus>(`${this.base}/api/workers`);
  }

  getLogs(lines = 100): Observable<LogResponse> {
    return this.http.get<LogResponse>(`${this.base}/api/logs`, { params: { lines } });
  }
}
