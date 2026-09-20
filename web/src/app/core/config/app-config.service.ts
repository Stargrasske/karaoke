import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export interface RuntimeConfig {
  libraryApiUrl: string;
  controlApiUrl: string;
}

const DEFAULT_CONFIG: RuntimeConfig = {
  libraryApiUrl: 'http://localhost:8000',
  controlApiUrl: 'http://127.0.0.1:8765'
};

/** Loads deployment-specific API URLs from /config.json at bootstrap so the same build can target different hosts. */
@Injectable({ providedIn: 'root' })
export class AppConfigService {
  private config: RuntimeConfig = DEFAULT_CONFIG;

  constructor(private http: HttpClient) {}

  async load(): Promise<void> {
    try {
      const loaded = await firstValueFrom(this.http.get<RuntimeConfig>('/config.json'));
      this.config = { ...DEFAULT_CONFIG, ...loaded };
    } catch {
      this.config = DEFAULT_CONFIG;
    }
  }

  get libraryApiUrl(): string {
    return this.config.libraryApiUrl;
  }

  get controlApiUrl(): string {
    return this.config.controlApiUrl;
  }
}
