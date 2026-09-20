import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { catchError, finalize, of, Subscription, switchMap, timer } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { LibraryApiService } from '../../core/api/library-api.service';
import { LyricLine, PlaybackState, QueueItem } from '../../core/models';
import { CaptureStateService } from '../../core/recording/capture-state.service';

@Component({
  selector: 'app-queue',
  standalone: true,
  imports: [FormsModule],
  template: `
    <section class="widget">
      @if (detectedTitle) {
        <section class="detected-song" aria-label="Detected song">
          <div>
            <span class="detected-song__label">Detected now</span>
            <strong>{{ detectedTitle }}</strong>
            <span class="widget__meta">{{ detectedArtist || 'Unknown artist' }} @if (detected?.album) { · {{ detected?.album }} }</span>
          </div>
          <div class="widget__actions">
            @if (detected?.key) { <span class="widget__pill">{{ detected?.key }}</span> }
            @if (detected?.bpm) { <span class="widget__pill">{{ detected?.bpm }} BPM</span> }
            <span class="widget__pill">{{ detectedStatus }}</span>
          </div>
        </section>
        @if (hasSynchronizedLyrics) {
          <section class="detected-lyrics detected-lyrics--live" aria-label="Synchronized detected song lyrics">
            <div class="detected-lyrics__heading">
              <strong>Live lyrics</strong>
              <div class="sync-controls">
                <span class="episode-badge" [class.episode-badge--music]="lyricEpisode.kind !== 'vocals'">{{ lyricEpisode.label }}</span>
                <button type="button" (click)="nudgeLyrics(-0.5)" title="Lyrics are ahead; move them later">Later</button>
                <button type="button" (click)="resetLyricsNudge()" title="Reset lyric timing adjustment">{{ lyricNudge >= 0 ? '+' : '' }}{{ lyricNudge.toFixed(1) }}s</button>
                <button type="button" (click)="nudgeLyrics(0.5)" title="Lyrics are behind; move them earlier">Earlier</button>
                @if (nextLineIn !== null) {
                  <span class="widget__pill">Next {{ nextLineIn }}s</span>
                }
              </div>
            </div>
            @if (lyricEpisode.kind !== 'vocals') {
              <div class="musical-episode">
                <span aria-hidden="true">♪</span>
                <strong>{{ lyricEpisode.label }}</strong>
                @if (episodeRemaining !== null) { <span>{{ episodeRemaining }}s remaining</span> }
              </div>
            }
            @for (line of visibleDetectedLines; track line.index) {
              <p
                class="detected-lyrics__line"
                [class.detected-lyrics__line--active]="line.index === activeLyricIndex"
                [class.detected-lyrics__line--past]="line.index < activeLyricIndex"
              >
                @if (line.index === activeLyricIndex) {
                  @for (word of words(line); track $index) {
                    <span [class.detected-lyrics__word--active]="$index === activeWordIndex(line)">{{ word }}</span>{{ ' ' }}
                  }
                } @else {
                  {{ line.text }}
                }
              </p>
            }
          </section>
        } @else if (detectedLyrics) {
          <section class="detected-lyrics" aria-label="Detected song lyrics">
            <div class="detected-lyrics__heading">
              <strong>Lyrics</strong>
              <span class="widget__pill">{{ lyricsSource }}</span>
            </div>
            <pre>{{ detectedLyrics }}</pre>
          </section>
        } @else if (lyricsLoading) {
          <p class="widget__meta">Finding lyrics…</p>
        } @else if (lyricsError) {
          <p class="widget__meta widget__state--error">{{ lyricsError }}</p>
        }
      } @else {
        <p class="detected-song detected-song--idle">Listening for a detected song…</p>
      }
      <form class="widget__toolbar" (ngSubmit)="enqueue()">
        <input name="artist" [(ngModel)]="artist" placeholder="Artist" aria-label="Artist" />
        <input name="title" [(ngModel)]="title" placeholder="Title" aria-label="Title" />
        <button type="submit" [disabled]="busy || !artist.trim() || !title.trim()">Add</button>
      </form>
      @if (loading) {
        <p class="widget__state">Loading queue…</p>
      } @else if (error) {
        <p class="widget__state widget__state--error">{{ error }} <button type="button" (click)="load()">Retry</button></p>
      } @else if (!items.length) {
        <p class="widget__state">The queue is empty.</p>
      } @else {
        <div class="widget__list">
          @for (item of items; track item.index) {
            <div class="widget__row">
              <div><strong>{{ item.title }}</strong><div class="widget__meta">{{ item.artist }}</div></div>
              <div class="widget__actions">
                <button type="button" (click)="move(item.index, item.index - 1)" [disabled]="busy || item.index === 0" title="Move up">Up</button>
                <button type="button" (click)="move(item.index, item.index + 1)" [disabled]="busy || item.index === items.length - 1" title="Move down">Down</button>
                <button class="widget__danger" type="button" (click)="remove(item.index)" [disabled]="busy">Remove</button>
              </div>
            </div>
          }
        </div>
      }
    </section>
  `,
  styleUrls: ['../widget.shared.scss', './queue.component.scss']
})
export class QueueComponent implements OnInit, OnDestroy {
  @Input() config?: Record<string, unknown>;
  items: QueueItem[] = [];
  artist = '';
  title = '';
  loading = true;
  busy = false;
  error = '';
  detected?: PlaybackState;
  detectedLyrics = '';
  lyricsSource = '';
  lyricsLoading = false;
  lyricsError = '';
  timedLyrics: LyricLine[] = [];
  recordingTrackStartWall?: number;
  lyricNudge = 0;
  private lyricsKey = '';
  private recordingClockLoading = false;
  private detectionSubscription?: Subscription;

  constructor(
    private controlApi: ControlApiService,
    private libraryApi: LibraryApiService,
    private captureState: CaptureStateService
  ) {}

  get detectedTitle(): string {
    return this.detected?.title || this.captureState.activeSession()?.detected_title || '';
  }

  get detectedArtist(): string {
    return this.detected?.artist || this.captureState.activeSession()?.detected_artist || '';
  }

  get detectedStatus(): string {
    return this.captureState.isRecording() ? 'Listening' : this.detected?.status || 'Waiting';
  }

  get hasSynchronizedLyrics(): boolean {
    return this.synchronizedLines.length > 0 && this.lyricPosition >= 0;
  }

  get visibleDetectedLines(): LyricLine[] {
    const lines = this.synchronizedLines;
    if (!lines.length) return [];
    const active = this.activeLyricIndex;
    const start = active < 0 ? 0 : Math.max(0, active - 1);
    return lines.slice(start, start + 4);
  }

  get synchronizedLines(): LyricLine[] {
    return this.detected?.lines.length ? this.detected.lines : this.timedLyrics;
  }

  get lyricPosition(): number {
    if (this.detected?.lines.length) return Math.max(0, this.detected.position_s + this.lyricNudge);
    return this.recordingTrackStartWall === undefined
      ? -1
      : Math.max(0, Date.now() / 1000 - this.recordingTrackStartWall + this.lyricNudge);
  }

  get activeLyricIndex(): number {
    return this.lyricEpisode.kind === 'vocals' ? (this.lyricEpisode.lineIndex ?? -1) : -1;
  }

  get nextLineIn(): number | null {
    const next = this.synchronizedLines.find((line) => line.time > this.lyricPosition);
    return next ? Math.max(0, Math.round((next.time - this.lyricPosition) * 10) / 10) : null;
  }

  get lyricEpisode(): { kind: 'intro' | 'vocals' | 'instrumental' | 'outro' | 'unknown'; label: string; start?: number; end?: number; lineIndex?: number } {
    const lines = this.synchronizedLines;
    const position = this.lyricPosition;
    if (!lines.length || position < 0) return { kind: 'unknown', label: 'Waiting for timing' };
    if (position < lines[0].time) return { kind: 'intro', label: 'Instrumental intro', start: 0, end: lines[0].time };

    let index = -1;
    for (const line of lines) {
      if (line.time <= position) index = line.index;
      else break;
    }
    if (index < 0) return { kind: 'unknown', label: 'Waiting for timing' };
    const line = lines[index];
    const next = lines[index + 1];
    const wordCount = Math.max(1, this.words(line).length);
    const vocalEnd = Math.min(
      line.end ?? Number.POSITIVE_INFINITY,
      line.time + Math.min(12, Math.max(1.5, wordCount * 0.7)),
      next?.time ?? Number.POSITIVE_INFINITY
    );
    if (position <= vocalEnd) return { kind: 'vocals', label: 'Vocals', start: line.time, end: vocalEnd, lineIndex: index };
    if (next && next.time - vocalEnd >= 6) return { kind: 'instrumental', label: 'Instrumental break', start: vocalEnd, end: next.time };
    if (!next) return { kind: 'outro', label: 'Instrumental outro', start: vocalEnd };
    return { kind: 'vocals', label: 'Vocals', start: line.time, end: next.time, lineIndex: index };
  }

  get episodeRemaining(): number | null {
    const end = this.lyricEpisode.end;
    return end === undefined ? null : Math.max(0, Math.round((end - this.lyricPosition) * 10) / 10);
  }

  ngOnInit(): void {
    this.load();
    this.detectionSubscription = timer(0, 2000).pipe(
      switchMap(() => this.controlApi.getStageState().pipe(catchError(() => of(null))))
    ).subscribe((state) => {
      if (state) this.detected = state;
      this.loadDetectedLyrics();
      this.loadRecordingClock();
    });
  }

  ngOnDestroy(): void { this.detectionSubscription?.unsubscribe(); }

  private loadDetectedLyrics(): void {
    const artist = this.detectedArtist.trim();
    const title = this.detectedTitle.trim();
    const key = `${artist}\u0000${title}`;
    if (!artist || !title || key === this.lyricsKey) return;

    this.lyricsKey = key;
    this.lyricNudge = this.loadLyricsNudge(key);
    this.detectedLyrics = '';
    this.lyricsError = '';
    this.lyricsLoading = true;
    this.libraryApi.lookupLyrics(artist, title)
      .pipe(finalize(() => { this.lyricsLoading = false; }))
      .subscribe({
        next: (detail) => {
          this.lyricsSource = detail.lyrics?.source || 'cached';
          this.detectedLyrics = this.formatLyrics(
            detail.lyrics?.plain || detail.lyrics?.synced_raw || ''
          );
          this.timedLyrics = this.parseSyncedLyrics(detail.lyrics?.synced_raw || '');
          if (!this.detectedLyrics) this.lyricsError = 'No lyrics available for this song.';
        },
        error: (error) => {
          this.lyricsError = error?.error?.detail || 'Lyrics lookup failed.';
        }
      });
  }

  private formatLyrics(value: string): string {
    return value
      .split(/\r?\n/)
      .map((line) => line.replace(/^(?:\[[^\]]+\])+/, '').trim())
      .filter(Boolean)
      .join('\n');
  }

  nudgeLyrics(delta: number): void {
    this.lyricNudge = Math.round((this.lyricNudge + delta) * 10) / 10;
    this.saveLyricsNudge();
  }

  resetLyricsNudge(): void {
    this.lyricNudge = 0;
    this.saveLyricsNudge();
  }

  private loadLyricsNudge(key: string): number {
    const raw = localStorage.getItem(`karaoke.lyric-nudge.${encodeURIComponent(key)}`);
    const value = raw === null ? 0 : Number(raw);
    return Number.isFinite(value) ? value : 0;
  }

  private saveLyricsNudge(): void {
    if (!this.lyricsKey) return;
    localStorage.setItem(
      `karaoke.lyric-nudge.${encodeURIComponent(this.lyricsKey)}`,
      String(this.lyricNudge)
    );
  }

  private parseSyncedLyrics(value: string): LyricLine[] {
    const lines: LyricLine[] = [];
    for (const rawLine of value.split(/\r?\n/)) {
      const match = rawLine.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)$/);
      if (!match) continue;
      const text = match[3].replace(/<\d+:\d+(?:\.\d+)?>/g, '').trim();
      if (!text) continue;
      const wordTimes = [...match[3].matchAll(/<(\d+):(\d+(?:\.\d+)?)>/g)]
        .map((word) => Number(word[1]) * 60 + Number(word[2]));
      lines.push({
        index: lines.length,
        time: Number(match[1]) * 60 + Number(match[2]),
        text,
        words: wordTimes
      });
    }
    return lines;
  }

  private loadRecordingClock(): void {
    const session = this.captureState.activeSession();
    if (!session || this.recordingClockLoading) {
      if (!session) this.recordingTrackStartWall = undefined;
      return;
    }
    this.recordingClockLoading = true;
    this.libraryApi.getRecording(session.recording_id)
      .pipe(finalize(() => { this.recordingClockLoading = false; }))
      .subscribe({
        next: (recording) => {
          const matching = [...(recording.tracks || [])].reverse().find((track) =>
            track.artist === this.detectedArtist && track.title === this.detectedTitle
          );
          this.recordingTrackStartWall = matching?.start_wall;
        }
      });
  }

  words(line: LyricLine): string[] {
    return line.text.trim().split(/\s+/).filter(Boolean);
  }

  activeWordIndex(line: LyricLine): number {
    if (line.index !== this.activeLyricIndex) return -1;
    if (line.words?.length) {
      let index = 0;
      for (let candidate = 0; candidate < line.words.length; candidate += 1) {
        if (line.words[candidate] <= this.lyricPosition) index = candidate;
        else break;
      }
      return index;
    }

    const words = this.words(line);
    if (!words.length) return -1;
    const nextLine = this.synchronizedLines[line.index + 1];
    const naturalEnd = line.end ?? nextLine?.time ?? line.time + 4;
    const end = Math.min(naturalEnd, line.time + 8);
    const duration = Math.max(0.1, end - line.time);
    const fraction = Math.min(0.999999, Math.max(0, (this.lyricPosition - line.time) / duration));
    return Math.min(words.length - 1, Math.floor(fraction * words.length));
  }

  load(): void {
    this.loading = true;
    this.error = '';
    this.controlApi.getQueue().pipe(finalize(() => { this.loading = false; })).subscribe({
      next: (response) => { this.items = response.items; },
      error: (error) => { this.error = error?.error?.detail || 'Queue is unavailable.'; }
    });
  }

  enqueue(): void {
    this.mutate(this.controlApi.enqueue({ artist: this.artist.trim(), title: this.title.trim() }), () => {
      this.artist = '';
      this.title = '';
    });
  }

  remove(index: number): void { this.mutate(this.controlApi.removeFromQueue(index)); }

  move(fromIndex: number, toIndex: number): void {
    this.mutate(this.controlApi.reorderQueue({ from_index: fromIndex, to_index: toIndex }));
  }

  private mutate(request: import('rxjs').Observable<unknown>, success?: () => void): void {
    this.busy = true;
    this.error = '';
    request.pipe(finalize(() => { this.busy = false; })).subscribe({
      next: () => { success?.(); this.load(); },
      error: (error) => { this.error = error?.error?.detail || 'Queue update failed.'; }
    });
  }
}
