import { DecimalPipe } from '@angular/common';
import { Component, Input, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { LibraryApiService } from '../../core/api/library-api.service';
import { SoundsLikeResult, StatsSummary, Track, TrackDetail } from '../../core/models';

@Component({
  selector: 'app-library',
  standalone: true,
  imports: [DecimalPipe, FormsModule],
  template: `
    <section class="widget">
      <form class="widget__toolbar" (ngSubmit)="search()">
        <input name="query" [(ngModel)]="query" placeholder="Search tracks" aria-label="Search tracks" />
        <button type="submit" [disabled]="loading">Search</button>
        <button type="button" (click)="clear()" [disabled]="loading || !query">Clear</button>
      </form>
      <form class="lyrics-lookup" (ngSubmit)="lookupLyrics()">
        <strong>Find lyrics online</strong>
        <input name="lyricArtist" [(ngModel)]="lyricArtist" placeholder="Artist" aria-label="Lyrics artist" />
        <input name="lyricTitle" [(ngModel)]="lyricTitle" placeholder="Song title" aria-label="Lyrics song title" />
        <button class="widget__primary" type="submit" [disabled]="busy || !lyricArtist.trim() || !lyricTitle.trim()">Find lyrics</button>
      </form>
      @if (lookupError) { <p class="widget__meta widget__state--error">{{ lookupError }}</p> }
      @if (loading) {
        <p class="widget__state">Searching library…</p>
      } @else if (error) {
        <p class="widget__state widget__state--error">{{ error }}</p>
      } @else if (!tracks.length) {
        <p class="widget__state">No tracks found. Scan a folder from Operations to add music.</p>
      } @else {
        <div class="widget__list">
          @for (track of tracks; track track.track_id) {
            <div class="widget__row">
              <div>
                <strong>{{ track.title }}</strong>
                <div class="widget__meta">{{ track.artist }} @if (track.key || track.bpm) { · {{ track.key || '—' }} · {{ track.bpm || '—' }} BPM }</div>
              </div>
              <div class="widget__actions">
                <button type="button" (click)="inspect(track)" [disabled]="busy">Details</button>
                <button type="button" (click)="enqueue(track)" [disabled]="busy">Queue</button>
                <button type="button" (click)="play(track)" [disabled]="busy || !track.url">Play</button>
              </div>
            </div>
          }
        </div>
      }
      @if (selected) {
        <details open>
          <summary>{{ selected.artist }} — {{ selected.title }}</summary>
          <p class="widget__meta">{{ selected.album || 'No album' }} · {{ selected.genre || 'No genre' }} · played {{ selected.play_count }} time(s)</p>
          <div class="widget__actions">
            <button type="button" (click)="findSource(selected)" [disabled]="busy">Find source</button>
            <button type="button" (click)="loadSimilar(selected)" [disabled]="busy">Sounds like</button>
            <button type="button" (click)="suggestQueue(selected)" [disabled]="busy">Suggest queue</button>
          </div>
          <section class="songbook-lyrics" aria-label="Lyrics">
            <div class="songbook-lyrics__heading">
              <strong>Lyrics</strong>
              @if (selected.lyrics?.source) { <span class="widget__pill">{{ selected.lyrics?.source }}</span> }
            </div>
            @if (selectedLyrics) {
              <pre>{{ selectedLyrics }}</pre>
            } @else {
              <p class="widget__state">No lyrics cached for this song.</p>
            }
          </section>
          @for (similar of similarTracks; track similar.track_id) {
            <div class="widget__row"><span>{{ similar.artist }} — {{ similar.title }}</span><span>{{ similar.score | number:'1.2-2' }}</span></div>
          }
        </details>
      }
      @if (stats) { <p class="widget__meta">Statistics loaded · {{ statsKeys }} sections</p> }
      @if (message) { <p class="widget__meta">{{ message }}</p> }
    </section>
  `,
  styleUrls: ['../widget.shared.scss', './library.component.scss']
})
export class LibraryComponent implements OnInit {
  @Input() config?: Record<string, unknown>;
  tracks: Track[] = [];
  selected?: TrackDetail;
  similarTracks: SoundsLikeResult[] = [];
  stats?: StatsSummary;
  query = '';
  lyricArtist = '';
  lyricTitle = '';
  lookupError = '';
  loading = false;
  busy = false;
  error = '';
  message = '';

  constructor(
    private libraryApi: LibraryApiService,
    private controlApi: ControlApiService
  ) {}

  get statsKeys(): number { return this.stats ? Object.keys(this.stats).length : 0; }

  get selectedLyrics(): string {
    const lyrics = this.selected?.lyrics;
    if (lyrics?.plain?.trim()) return lyrics.plain.trim();
    if (!lyrics?.synced_raw?.trim()) return '';
    return lyrics.synced_raw
      .split(/\r?\n/)
      .map((line) => line.replace(/^(?:\[[^\]]+\])+/, '').trim())
      .filter(Boolean)
      .join('\n');
  }

  ngOnInit(): void {
    this.search();
    this.libraryApi.getStats().subscribe({ next: (stats) => { this.stats = stats; } });
  }

  search(): void {
    this.loading = true;
    this.error = '';
    this.libraryApi.listTracks({ q: this.query.trim() || undefined, limit: 50 })
      .pipe(finalize(() => { this.loading = false; }))
      .subscribe({
        next: (tracks) => { this.tracks = tracks; },
        error: (error) => { this.error = error?.error?.detail || 'Library is unavailable.'; }
      });
  }

  clear(): void { this.query = ''; this.search(); }

  lookupLyrics(): void {
    this.busy = true;
    this.lookupError = '';
    this.libraryApi.lookupLyrics(this.lyricArtist.trim(), this.lyricTitle.trim())
      .pipe(finalize(() => { this.busy = false; }))
      .subscribe({
        next: (detail) => {
          this.selected = detail;
          this.query = detail.title;
          this.message = 'Lyrics found and added to Songbook.';
          this.search();
        },
        error: (error) => {
          this.lookupError = error?.error?.detail || 'Lyrics lookup failed.';
        }
      });
  }

  enqueue(track: Track): void {
    this.action(this.controlApi.enqueue({ artist: track.artist, title: track.title, url: track.url }), 'Added to queue.');
  }

  play(track: Track): void {
    this.action(this.controlApi.play({ url: track.url || undefined, artist: track.artist, title: track.title }), 'Playback launched.');
  }

  inspect(track: Track): void {
    this.busy = true;
    this.libraryApi.getTrack(track.track_id).pipe(finalize(() => { this.busy = false; })).subscribe({
      next: (detail) => { this.selected = detail; this.libraryApi.getTrackAnalysisHistory(track.track_id).subscribe(); },
      error: (error) => { this.error = error?.error?.detail || 'Track detail is unavailable.'; }
    });
  }

  findSource(track: Track): void { this.action(this.libraryApi.findSource(track.track_id), 'Source lookup complete.'); }

  loadSimilar(track: Track): void {
    this.busy = true;
    this.libraryApi.getSoundsLike(track.track_id).pipe(finalize(() => { this.busy = false; })).subscribe({
      next: (results) => { this.similarTracks = results; },
      error: (error) => { this.error = error?.error?.detail || 'Recommendations are unavailable.'; }
    });
  }

  suggestQueue(track: Track): void {
    this.busy = true;
    this.libraryApi.suggestQueue([track.track_id]).pipe(finalize(() => { this.busy = false; })).subscribe({
      next: (results) => { this.similarTracks = results; this.message = 'Queue suggestions loaded.'; },
      error: (error) => { this.error = error?.error?.detail || 'Queue suggestions are unavailable.'; }
    });
  }

  private action(request: import('rxjs').Observable<unknown>, message: string): void {
    this.busy = true;
    this.error = '';
    request.pipe(finalize(() => { this.busy = false; })).subscribe({
      next: () => { this.message = message; },
      error: (error) => { this.error = error?.error?.detail || 'Action failed.'; }
    });
  }
}
