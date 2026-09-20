import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { Subscription } from 'rxjs';

import { LyricLine, PlaybackState } from '../../core/models';
import { StageStreamService } from '../../core/realtime/stage-stream.service';

@Component({
  selector: 'app-now-playing',
  standalone: true,
  template: `
    <section class="widget" aria-live="polite">
      @if (state) {
        <div>
          <h2 class="widget__title">{{ state.title || 'Nothing playing' }}</h2>
          <p class="widget__meta">{{ state.artist || 'Waiting for a player' }} @if (state.album) { · {{ state.album }} }</p>
        </div>
        <div class="widget__progress" aria-hidden="true"><span [style.width.%]="progress"></span></div>
        <div class="widget__row">
          <span>{{ formatTime(state.position_s) }} / {{ formatTime(state.duration || 0) }}</span>
          <span class="widget__pill">{{ state.status }}</span>
        </div>
        <div class="widget__row">
          <span>{{ state.key || 'Key —' }}</span>
          <span>{{ state.bpm ? state.bpm + ' BPM' : 'Tempo —' }}</span>
        </div>
        @if (state.lines.length) {
          <section class="lyric-follow" aria-label="Live synchronized lyrics">
            @if (state.episode && state.episode.kind !== 'vocals') {
              <div class="lyric-follow__episode"><span aria-hidden="true">♪</span> {{ state.episode.label }}</div>
            } @else if (state.episode) {
              <span class="lyric-follow__episode-label">{{ state.episode.label }}</span>
            }
            @for (line of visibleLines; track line.index) {
              <p
                class="lyric-follow__line"
                [class.lyric-follow__line--active]="line.index === state.active_line_index"
                [class.lyric-follow__line--past]="line.index < state.active_line_index"
              >
                @if (line.index === state.active_line_index) {
                  @for (word of words(line); track $index) {
                    <span [class.lyric-follow__word--active]="$index === activeWordIndex(line)">{{ word }}</span>{{ ' ' }}
                  }
                } @else {
                  {{ line.text }}
                }
              </p>
            }
            @if (state.next_line_in !== null && state.next_line_in !== undefined) {
              <span class="lyric-follow__countdown">Next line in {{ state.next_line_in }}s</span>
            }
          </section>
        } @else {
          <p class="widget__state">Lyrics will appear when the detected song has synced lyrics.</p>
        }
      } @else if (error) {
        <p class="widget__state widget__state--error">{{ error }}</p>
      } @else {
        <p class="widget__state">Connecting to stage stream…</p>
      }
    </section>
  `,
  styleUrls: ['../widget.shared.scss', './now-playing.component.scss']
})
export class NowPlayingComponent implements OnInit, OnDestroy {
  @Input() config?: Record<string, unknown>;
  state?: PlaybackState;
  error = '';
  private subscription?: Subscription;

  constructor(private stageStream: StageStreamService) {}

  get progress(): number {
    if (!this.state?.duration) return 0;
    return Math.min(100, Math.max(0, (this.state.position_s / this.state.duration) * 100));
  }

  get visibleLines(): LyricLine[] {
    if (!this.state?.lines.length) return [];
    const active = this.state.active_line_index;
    const start = active < 0 ? 0 : Math.max(0, active - 1);
    return this.state.lines.slice(start, start + 4);
  }

  ngOnInit(): void {
    this.subscription = this.stageStream.connect().subscribe({
      next: (state) => { this.state = state; this.error = ''; },
      error: () => { this.error = 'Stage stream is unavailable.'; }
    });
  }

  ngOnDestroy(): void {
    this.subscription?.unsubscribe();
  }

  formatTime(value: number): string {
    const seconds = Math.max(0, Math.floor(value));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }

  words(line: LyricLine): string[] {
    return line.text.trim().split(/\s+/).filter(Boolean);
  }

  activeWordIndex(line: LyricLine): number {
    if (!this.state || line.index !== this.state.active_line_index) return -1;
    if (line.words?.length) {
      let index = 0;
      for (let candidate = 0; candidate < line.words.length; candidate += 1) {
        if (line.words[candidate] <= this.state.position_s) index = candidate;
        else break;
      }
      return index;
    }

    const words = this.words(line);
    if (!words.length) return -1;
    const nextLine = this.state.lines[line.index + 1];
    const naturalEnd = line.end ?? nextLine?.time ?? line.time + 4;
    const end = Math.min(naturalEnd, line.time + 8);
    const duration = Math.max(0.1, end - line.time);
    const fraction = Math.min(0.999999, Math.max(0, (this.state.position_s - line.time) / duration));
    return Math.min(words.length - 1, Math.floor(fraction * words.length));
  }
}
