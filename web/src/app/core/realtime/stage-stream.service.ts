import { Injectable, NgZone, OnDestroy } from '@angular/core';
import { Observable } from 'rxjs';

import { AppConfigService } from '../config/app-config.service';
import { PlaybackState } from '../models';

const RECONNECT_DELAY_MS = 2000;

/** Wraps /api/stage/stream (SSE, ~10Hz) as a hot Observable<PlaybackState> with auto-reconnect. */
@Injectable({ providedIn: 'root' })
export class StageStreamService implements OnDestroy {
  private eventSource: EventSource | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private config: AppConfigService,
    private zone: NgZone
  ) {}

  connect(): Observable<PlaybackState> {
    return new Observable<PlaybackState>((subscriber) => {
      const open = () => {
        const url = `${this.config.controlApiUrl}/api/stage/stream`;
        const source = new EventSource(url);
        this.eventSource = source;

        source.onmessage = (event: MessageEvent<string>) => {
          this.zone.run(() => {
            try {
              subscriber.next(JSON.parse(event.data) as PlaybackState);
            } catch {
              // ignore malformed frame, next tick will self-correct
            }
          });
        };

        source.onerror = () => {
          source.close();
          this.zone.run(() => {
            this.reconnectTimer = setTimeout(open, RECONNECT_DELAY_MS);
          });
        };
      };

      open();

      return () => {
        this.eventSource?.close();
        this.eventSource = null;
        if (this.reconnectTimer) {
          clearTimeout(this.reconnectTimer);
          this.reconnectTimer = null;
        }
      };
    });
  }

  ngOnDestroy(): void {
    this.eventSource?.close();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }
  }
}
