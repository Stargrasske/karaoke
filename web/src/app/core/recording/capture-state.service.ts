import { computed, Injectable, signal } from '@angular/core';
import { catchError, of, switchMap, timer } from 'rxjs';

import { ControlApiService } from '../api/control-api.service';
import { RecordDevicesResponse, RecordStatusResponse } from '../models';

const SOURCE_KEY = 'karaoke.capture.source';

@Injectable({ providedIn: 'root' })
export class CaptureStateService {
  readonly status = signal<RecordStatusResponse>({ recording: [], count: 0 });
  readonly capabilities = signal<RecordDevicesResponse | null>(null);
  readonly reachable = signal(true);
  readonly activeSession = computed(() => this.status().recording[0] ?? null);
  readonly isRecording = computed(() => this.status().count > 0);
  readonly levelPercent = computed(() => {
    const level = this.activeSession()?.level_db;
    if (level === undefined || level === null) return 0;
    return Math.min(100, Math.max(0, ((level + 60) / 60) * 100));
  });
  readonly levelLabel = computed(() => {
    const level = this.activeSession()?.level_db;
    return level === undefined || level === null ? 'meter warming up' : `${level.toFixed(1)} dB`;
  });
  readonly selectedSource = signal(localStorage.getItem(SOURCE_KEY) ?? '');

  constructor(private controlApi: ControlApiService) {
    this.refreshCapabilities();
    timer(0, 2000).pipe(
      switchMap(() => this.controlApi.recordStatus().pipe(catchError(() => of(null))))
    ).subscribe((status) => {
      this.reachable.set(status !== null);
      if (status) this.status.set(status);
    });
  }

  setSource(source: string): void {
    this.selectedSource.set(source);
    if (source) localStorage.setItem(SOURCE_KEY, source);
    else localStorage.removeItem(SOURCE_KEY);
  }

  refreshStatus(): void {
    this.controlApi.recordStatus().subscribe({
      next: (status) => { this.status.set(status); this.reachable.set(true); },
      error: () => { this.reachable.set(false); }
    });
  }

  refreshCapabilities(): void {
    this.controlApi.recordingDevices().subscribe({
      next: (capabilities) => {
        this.capabilities.set(capabilities);
        if (this.selectedSource() && !capabilities.devices.includes(this.selectedSource())) {
          this.setSource('');
        }
      }
    });
  }
}
