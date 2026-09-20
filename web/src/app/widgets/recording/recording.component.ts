import { DecimalPipe } from '@angular/common';
import { Component, Input, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, forkJoin } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { LibraryApiService } from '../../core/api/library-api.service';
import { Recording } from '../../core/models';
import { CaptureStateService } from '../../core/recording/capture-state.service';

interface RadioSessionSummary {
  session_id: number;
  station?: string;
  title?: string;
}

@Component({
  selector: 'app-recording',
  standalone: true,
  imports: [DecimalPipe, FormsModule],
  template: `
    <section class="widget">
      <div class="widget__toolbar">
        <select [ngModel]="source" (ngModelChange)="sourceChanged($event)" aria-label="Recording source">
          <option value="" [disabled]="platform === 'windows'">{{ platform === 'windows' ? 'Select a microphone' : 'Automatic output source' }}</option>
          @for (device of devices; track device) { <option [value]="device">{{ device }}</option> }
        </select>
        <input [(ngModel)]="note" placeholder="Session note" aria-label="Session note" />
        <label><input type="checkbox" [(ngModel)]="keepAudio" /> Keep audio</label>
        <button class="widget__primary" type="button" (click)="start()" [disabled]="!canStart" [title]="startHint">Start recording</button>
        <button type="button" (click)="stop()" [disabled]="busy || activeCount === 0" [title]="activeCount ? 'Stop the active capture' : 'No recording is active'">Stop</button>
      </div>
      <p class="widget__meta">{{ sourceHint }} · {{ activeCount }} active capture(s). Recording starts only when you press Start.</p>
      <div class="capture-flow" aria-label="Capture pipeline">
        <span>{{ capabilities?.capture_mode || 'audio source' }}</span>
        <span aria-hidden="true">→</span>
        <span>{{ capabilities?.capture_backend || 'capture backend' }}</span>
        <span aria-hidden="true">→</span>
        <span>FFmpeg FLAC segments</span>
        <span aria-hidden="true">→</span>
        <span>{{ capabilities?.identification_backend || 'identifier unknown' }}</span>
      </div>
      @if (activeSession) {
        <div class="capture-live">
          <strong>Recording {{ activeSession.recording_id }}</strong>
          <span>{{ activeSession.elapsed_s | number:'1.0-0' }}s</span>
          <span>{{ activeSession.audio_bytes | number }} bytes</span>
          <span>{{ activeSession.identified }}/{{ activeSession.marks }} identified</span>
          <span>{{ activeSession.source }}</span>
        </div>
        <div class="input-level">
          <div class="input-level__label">
            <strong>Input level</strong>
            <span>{{ captureState.levelLabel() }}</span>
          </div>
          <div class="input-meter" role="meter" aria-label="Microphone input level" aria-valuemin="0" aria-valuemax="100" [attr.aria-valuenow]="captureState.levelPercent()">
            <span [style.width.%]="captureState.levelPercent()"></span>
          </div>
          <p class="widget__meta">Speak or play audio near the selected microphone; the bar should move.</p>
        </div>
      }
      <details>
        <summary>Sample key and tempo</summary>
        <div class="widget__toolbar">
          <input [(ngModel)]="sampleArtist" placeholder="Artist (optional)" />
          <input [(ngModel)]="sampleTitle" placeholder="Title (optional)" />
          <input [(ngModel)]="sampleSeconds" type="number" min="5" max="120" aria-label="Sample seconds" />
          <button type="button" (click)="sample()" [disabled]="busy || (platform === 'windows' && !source)">Capture sample</button>
        </div>
      </details>
      @if (loading) { <p class="widget__state">Loading recordings…</p> }
      @else if (!recordings.length) { <p class="widget__state">No recording sessions yet.</p> }
      @else {
        <div class="widget__list">
          @for (recording of recordings; track recording.recording_id) {
            <div class="widget__row">
              <div><strong>Recording {{ recording.recording_id }}</strong><div class="widget__meta">{{ recording.status }} · {{ recording.captured_s | number:'1.0-0' }}s · {{ recording.identified }} identified</div></div>
              <div class="widget__actions">
                <button type="button" (click)="toggleKeepAudio(recording)" [disabled]="busy">{{ recording.keep_audio ? 'Discard after analysis' : 'Keep audio' }}</button>
                @if (recording.tracks?.length) { <button type="button" (click)="playTrack(recording, 0)">Play first track</button> }
                <button type="button" (click)="analyse(recording)" [disabled]="busy || recording.running">Analyse</button>
                <button class="widget__danger" type="button" (click)="discard(recording)" [disabled]="busy || recording.running">Delete audio</button>
              </div>
            </div>
          }
        </div>
      }
      <details>
        <summary>Radio sessions ({{ radioSessions.length }})</summary>
        @for (session of radioSessions; track session.session_id) {
          <div class="widget__row">
            <span>{{ session.station || session.title || 'Session' }} {{ session.session_id }}</span>
            <button type="button" (click)="importRadio(session)" [disabled]="busy">Import</button>
          </div>
        }
      </details>
      @if (message) { <p class="widget__meta">{{ message }}</p> }
      @if (error) { <p class="widget__state widget__state--error">{{ error }}</p> }
    </section>
  `,
  styleUrl: '../widget.shared.scss'
})
export class RecordingComponent implements OnInit {
  @Input() config?: Record<string, unknown>;
  recordings: Recording[] = [];
  radioSessions: RadioSessionSummary[] = [];
  note = '';
  keepAudio = false;
  sampleArtist = '';
  sampleTitle = '';
  sampleSeconds = 15;
  loading = true;
  busy = false;
  message = '';
  error = '';

  constructor(
    private controlApi: ControlApiService,
    private libraryApi: LibraryApiService,
    public captureState: CaptureStateService
  ) {}

  get capabilities() { return this.captureState.capabilities(); }
  get devices(): string[] { return this.capabilities?.devices ?? []; }
  get platform(): string { return this.capabilities?.platform ?? 'unknown'; }
  get activeCount(): number { return this.captureState.status().count; }
  get activeSession() { return this.captureState.activeSession(); }
  get source(): string { return this.captureState.selectedSource(); }

  get sourceHint(): string {
    if (this.platform === 'windows') {
      return this.source ? `Windows microphone: ${this.source}` : 'Choose a Windows microphone';
    }
    return 'Automatic system-output capture';
  }

  get canStart(): boolean {
    return !this.busy && this.activeCount === 0 && (this.platform !== 'windows' || !!this.source);
  }

  get startHint(): string {
    if (this.platform === 'windows' && !this.source) return 'Select a microphone first';
    if (this.activeCount > 0) return 'A recording is already active';
    return 'Start recording from the selected source';
  }

  ngOnInit(): void { this.load(); }

  sourceChanged(source: string): void {
    this.captureState.setSource(source);
    this.error = '';
  }

  load(): void {
    this.loading = true;
    this.error = '';
    forkJoin({
      recordings: this.libraryApi.listRecordings({}),
      radio: this.libraryApi.listRadioSessions()
    }).pipe(finalize(() => { this.loading = false; })).subscribe({
      next: ({ recordings, radio }) => {
        this.recordings = recordings;
        this.radioSessions = radio.sessions as unknown as RadioSessionSummary[];
        this.captureState.refreshStatus();
        this.captureState.refreshCapabilities();
      },
      error: (error) => { this.error = error?.error?.detail || 'Recording services are unavailable.'; }
    });
  }

  start(): void { this.run(this.controlApi.startRecording(this.source || undefined, this.keepAudio, this.note || undefined), 'Recording started.'); }
  stop(): void { this.run(this.controlApi.stopRecording(), 'Recording stopped.'); }
  sample(): void { this.run(this.controlApi.sample({ artist: this.sampleArtist || undefined, title: this.sampleTitle || undefined, seconds: this.sampleSeconds, source: this.source || undefined }), 'Sample captured and analysed.'); }
  analyse(recording: Recording): void { this.run(this.controlApi.analyseRecording(recording.recording_id, recording.keep_audio), 'Analysis job started.'); }

  toggleKeepAudio(recording: Recording): void {
    this.run(this.libraryApi.updateRecording(recording.recording_id, { keep_audio: !recording.keep_audio }), 'Recording preference updated.');
  }

  playTrack(recording: Recording, index: number): void {
    window.open(this.controlApi.recordingTrackPageUrl(recording.recording_id, index), '_blank', 'noopener');
  }

  importRadio(session: RadioSessionSummary): void {
    this.libraryApi.getRadioSession(String(session.session_id)).subscribe({
      next: () => this.run(this.controlApi.importRadioSession(String(session.session_id)), 'Radio import started.'),
      error: (error) => { this.error = error?.error?.detail || 'Radio session is unavailable.'; }
    });
  }

  discard(recording: Recording): void {
    if (!confirm(`Delete retained audio for recording ${recording.recording_id}?`)) return;
    this.run(this.controlApi.deleteRecordingAudio(recording.recording_id), 'Recording audio deleted.');
  }

  private run(request: import('rxjs').Observable<unknown>, message: string): void {
    this.busy = true;
    this.error = '';
    request.pipe(finalize(() => { this.busy = false; })).subscribe({
      next: () => { this.message = message; this.captureState.refreshStatus(); this.load(); },
      error: (error) => { this.error = error?.error?.detail || 'Recording operation failed.'; }
    });
  }
}
