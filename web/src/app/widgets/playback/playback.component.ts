import { Component, Input, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, forkJoin } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { MprisPlayer, PlaySession } from '../../core/models';

@Component({
  selector: 'app-playback',
  standalone: true,
  imports: [FormsModule],
  template: `
    <section class="widget">
      <div>
        <h2 class="widget__title">Playback control</h2>
        <p class="widget__meta">{{ player?.player || 'No active player' }} · {{ player?.status || 'offline' }}</p>
      </div>
      <select [(ngModel)]="selectedPlayer" (change)="refreshPlayer()" aria-label="Target player">
        <option value="">Active player</option>
        @for (name of players; track name) { <option [value]="name">{{ name }}</option> }
      </select>
      <div class="widget__actions">
        <button type="button" (click)="previous()" [disabled]="busy" title="Previous track">Previous</button>
        <button class="widget__primary" type="button" (click)="playPause()" [disabled]="busy">Play / pause</button>
        <button type="button" (click)="next()" [disabled]="busy" title="Next track">Next</button>
      </div>
      <div class="widget__actions">
        <button type="button" (click)="seek(-10)" [disabled]="busy">−10s</button>
        <button type="button" (click)="seek(10)" [disabled]="busy">+10s</button>
        <button type="button" (click)="refresh()" [disabled]="busy">Refresh</button>
      </div>
      <form class="widget__toolbar" (ngSubmit)="launch()">
        <input name="url" [(ngModel)]="url" type="url" placeholder="Media URL" aria-label="Media URL" />
        <button type="submit" [disabled]="busy || !url.trim()">Launch</button>
      </form>
      @if (message) { <p class="widget__meta">{{ message }}</p> }
      @if (error) { <p class="widget__state widget__state--error">{{ error }}</p> }
      <details>
        <summary>Launch sessions ({{ sessions.length }})</summary>
        @for (session of sessions; track session.session_id) {
          <div class="widget__row">
            <span>{{ session.title || session.url || session.session_id }}</span>
            <button type="button" (click)="stopSession(session)" [disabled]="busy || session.status === 'stopped'">Stop</button>
          </div>
        }
      </details>
    </section>
  `,
  styleUrl: '../widget.shared.scss'
})
export class PlaybackComponent implements OnInit {
  @Input() config?: Record<string, unknown>;
  player?: MprisPlayer;
  players: string[] = [];
  sessions: PlaySession[] = [];
  selectedPlayer = '';
  url = '';
  busy = false;
  message = '';
  error = '';

  constructor(private controlApi: ControlApiService) {}

  ngOnInit(): void { this.refresh(); }

  refresh(): void {
    this.run(
      () => forkJoin({
        player: this.controlApi.getCurrentPlayer(this.selectedPlayer || undefined),
        players: this.controlApi.listPlayers(),
        sessions: this.controlApi.listPlaySessions()
      }),
      ({ player, players, sessions }) => {
        this.player = player;
        this.players = players.players;
        this.sessions = sessions.sessions;
      }
    );
  }

  refreshPlayer(): void { this.refresh(); }

  playPause(): void { this.runAction(() => this.controlApi.playPause(this.player?.player)); }
  previous(): void { this.runAction(() => this.controlApi.previous(this.player?.player)); }
  next(): void { this.runAction(() => this.controlApi.next(this.player?.player)); }
  seek(offset: number): void { this.runAction(() => this.controlApi.seek(offset, this.player?.player)); }

  launch(): void {
    this.run(() => this.controlApi.play({ url: this.url.trim() }), () => {
      this.message = 'Playback launched.';
      this.url = '';
    });
  }

  stopSession(session: PlaySession): void {
    if (!confirm(`Stop playback session ${session.title || session.session_id}?`)) return;
    this.run(() => this.controlApi.stopPlaySession(session.session_id), () => this.refresh());
  }

  private runAction(action: () => ReturnType<ControlApiService['playPause']>): void {
    this.run(action, () => this.refresh());
  }

  private run<T>(action: () => import('rxjs').Observable<T>, success: (value: T) => void): void {
    this.busy = true;
    this.error = '';
    this.message = '';
    action().pipe(finalize(() => { this.busy = false; })).subscribe({
      next: success,
      error: (error) => { this.error = error?.error?.detail || 'Playback request failed.'; }
    });
  }
}
