import { Component, Input, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, forkJoin } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { LibraryApiService } from '../../core/api/library-api.service';
import { AppConfigService } from '../../core/config/app-config.service';
import { WorkerStatus } from '../../core/models';

@Component({
  selector: 'app-operations',
  standalone: true,
  imports: [FormsModule],
  template: `
    <section class="widget">
      <div class="widget__row">
        <div><strong>Workers</strong><div class="widget__meta">{{ workers?.orchestrator || 'unavailable' }} · queue {{ workers?.queue_depth ?? 0 }}</div></div>
        <div class="widget__actions">
          <button type="button" (click)="scale(1)" [disabled]="busy">Start</button>
          <button type="button" (click)="scale(0)" [disabled]="busy">Stop</button>
          <button type="button" (click)="load()" [disabled]="busy">Refresh</button>
        </div>
      </div>
      <details open>
        <summary>Library ingestion</summary>
        <form class="widget__toolbar" (ngSubmit)="scan()">
          <input name="folder" [(ngModel)]="folder" placeholder="Folder path" aria-label="Folder path" />
          <label><input name="dryRun" type="checkbox" [(ngModel)]="dryRun" /> Dry run</label>
          <button type="submit" [disabled]="busy || !folder.trim()">Scan</button>
        </form>
      </details>
      <details>
        <summary>Audio cut</summary>
        <form class="widget__toolbar" (ngSubmit)="cut()">
          <input name="audioFile" [(ngModel)]="audioFile" placeholder="Audio file path" />
          <input name="startSeconds" [(ngModel)]="startSeconds" type="number" min="0" aria-label="Start seconds" />
          <input name="durationSeconds" [(ngModel)]="durationSeconds" type="number" min="0.1" aria-label="Duration seconds" />
          <button type="submit" [disabled]="busy || !audioFile.trim() || durationSeconds <= 0">Cut</button>
        </form>
      </details>
      <details>
        <summary>Player window</summary>
        <div class="widget__actions">
          <button type="button" (click)="inspectWindow()" [disabled]="busy">Inspect</button>
          <button type="button" (click)="windowState('focus')" [disabled]="busy">Focus</button>
          <button type="button" (click)="windowState('fullscreen')" [disabled]="busy">Fullscreen</button>
          <button type="button" (click)="windowState('normal')" [disabled]="busy">Windowed</button>
          <button type="button" (click)="restartWindow()" [disabled]="busy">Restart</button>
          <button type="button" (click)="openStage()">Open stage</button>
        </div>
      </details>
      <details>
        <summary>Job status</summary>
        <form class="widget__toolbar" (ngSubmit)="lookupJob()">
          <input name="jobId" [(ngModel)]="jobId" placeholder="Job ID" aria-label="Job ID" />
          <button type="submit" [disabled]="busy || !jobId.trim()">Check</button>
        </form>
      </details>
      <details>
        <summary>Recent errors ({{ logs.length }}) · events ({{ eventCount }})</summary>
        <div class="widget__list">
          @for (line of logs; track $index) { <div class="widget__meta">{{ line }}</div> }
          @if (!logs.length) { <p class="widget__state">No recent errors.</p> }
        </div>
      </details>
      @if (message) { <p class="widget__meta">{{ message }}</p> }
      @if (error) { <p class="widget__state widget__state--error">{{ error }}</p> }
    </section>
  `,
  styleUrl: '../widget.shared.scss'
})
export class OperationsComponent implements OnInit {
  @Input() config?: Record<string, unknown>;
  workers?: WorkerStatus;
  logs: string[] = [];
  eventCount = 0;
  folder = '';
  dryRun = true;
  audioFile = '';
  startSeconds = 0;
  durationSeconds = 30;
  jobId = '';
  busy = false;
  message = '';
  error = '';

  constructor(
    private controlApi: ControlApiService,
    private libraryApi: LibraryApiService,
    private configService: AppConfigService
  ) {}

  ngOnInit(): void { this.load(); }

  load(): void {
    this.busy = true;
    this.error = '';
    forkJoin({
      workers: this.controlApi.workersStatus(),
      errors: this.controlApi.recentErrors(100),
      events: this.controlApi.recentEvents(undefined, 100),
      libraryWorkers: this.libraryApi.getWorkers(),
      logs: this.libraryApi.getLogs(100)
    }).pipe(finalize(() => { this.busy = false; })).subscribe({
      next: ({ workers, errors, events }) => {
        this.workers = workers;
        this.logs = errors.logs;
        this.eventCount = events.count;
      },
      error: (error) => { this.error = error?.error?.detail || 'Operations data is unavailable.'; }
    });
  }

  scale(target: number): void {
    if (target === 0 && !confirm('Stop the background worker?')) return;
    this.run(this.controlApi.scaleWorkers(target), `Worker target set to ${target}.`, true);
  }

  scan(): void {
    this.run(this.controlApi.scanFolder({ dir: this.folder.trim(), dry_run: this.dryRun }), this.dryRun ? 'Scan preview complete.' : 'Folder scan started.');
  }

  cut(): void {
    this.run(this.controlApi.cutAudio({ file_path: this.audioFile.trim(), start_s: this.startSeconds, duration_s: this.durationSeconds }), 'Audio cut created.');
  }

  windowState(state: 'focus' | 'fullscreen' | 'normal'): void { this.run(this.controlApi.updatePlayerWindow({ state }), `Player window: ${state}.`); }

  restartWindow(): void {
    if (!confirm('Restart the kiosk player window?')) return;
    this.run(this.controlApi.restartPlayerWindow(), 'Player window restarted.');
  }

  inspectWindow(): void { this.run(this.controlApi.getPlayerWindow(), 'Player window information loaded.'); }

  lookupJob(): void {
    this.busy = true;
    this.error = '';
    this.controlApi.getJob(this.jobId.trim()).pipe(finalize(() => { this.busy = false; })).subscribe({
      next: (job) => { this.message = `Job ${job.job_id}: ${job.status}${job.error ? ` — ${job.error}` : ''}`; },
      error: (error) => { this.error = error?.error?.detail || 'Job is unavailable.'; }
    });
  }

  openStage(): void { window.open(`${this.configService.controlApiUrl}/stage`, '_blank', 'noopener'); }

  private run(request: import('rxjs').Observable<unknown>, message: string, refresh = false): void {
    this.busy = true;
    this.error = '';
    request.pipe(finalize(() => { this.busy = false; })).subscribe({
      next: () => { this.message = message; if (refresh) this.load(); },
      error: (error) => { this.error = error?.error?.detail || 'Operation failed.'; }
    });
  }
}
