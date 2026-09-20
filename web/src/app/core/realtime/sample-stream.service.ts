import { Injectable, NgZone } from '@angular/core';
import { Observable } from 'rxjs';

import { AppConfigService } from '../config/app-config.service';

export interface SampleProgressEvent {
  type: 'start' | 'progress' | 'complete' | 'error';
  data: Record<string, unknown>;
}

/** Wraps /api/sample/stream (SSE) for live key/BPM capture progress; one-shot per capture, no auto-reconnect. */
@Injectable({ providedIn: 'root' })
export class SampleStreamService {
  constructor(
    private config: AppConfigService,
    private zone: NgZone
  ) {}

  connect(artist: string, title: string, seconds?: number, source?: string): Observable<SampleProgressEvent> {
    return new Observable<SampleProgressEvent>((subscriber) => {
      const params = new URLSearchParams({ artist, title });
      if (seconds !== undefined) {
        params.set('seconds', String(seconds));
      }
      if (source) {
        params.set('source', source);
      }
      const url = `${this.config.controlApiUrl}/api/sample/stream?${params.toString()}`;
      const source = new EventSource(url);

      const handler = (type: SampleProgressEvent['type']) => (event: MessageEvent<string>) => {
        this.zone.run(() => {
          try {
            subscriber.next({ type, data: JSON.parse(event.data) });
          } catch {
            // ignore malformed frame
          }
          if (type === 'complete' || type === 'error') {
            source.close();
            subscriber.complete();
          }
        });
      };

      source.addEventListener('start', handler('start'));
      source.addEventListener('progress', handler('progress'));
      source.addEventListener('complete', handler('complete'));
      source.addEventListener('error', handler('error'));

      source.onerror = () => {
        source.close();
        this.zone.run(() => subscriber.error(new Error('sample stream connection error')));
      };

      return () => source.close();
    });
  }
}
