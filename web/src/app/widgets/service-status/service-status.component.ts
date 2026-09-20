import { Component, Input, OnInit } from '@angular/core';
import { forkJoin } from 'rxjs';

import { ControlApiService } from '../../core/api/control-api.service';
import { LibraryApiService } from '../../core/api/library-api.service';

@Component({
  selector: 'app-service-status',
  standalone: true,
  template: `
    <section class="widget" aria-live="polite">
      <div class="widget__row">
        <span>Library API</span>
        <span class="widget__pill" [class.widget__pill--online]="libraryOnline" [class.widget__pill--offline]="!loading && !libraryOnline">
          {{ loading ? 'checking' : libraryOnline ? 'online' : 'offline' }}
        </span>
      </div>
      <div class="widget__row">
        <span>Control API</span>
        <span class="widget__pill" [class.widget__pill--online]="controlOnline" [class.widget__pill--offline]="!loading && !controlOnline">
          {{ loading ? 'checking' : controlOnline ? 'online' : 'offline' }}
        </span>
      </div>
      @if (error) { <p class="widget__state widget__state--error">{{ error }}</p> }
      <button type="button" (click)="refresh()" [disabled]="loading">Refresh</button>
    </section>
  `,
  styleUrl: '../widget.shared.scss'
})
export class ServiceStatusComponent implements OnInit {
  @Input() config?: Record<string, unknown>;
  loading = true;
  libraryOnline = false;
  controlOnline = false;
  error = '';

  constructor(
    private libraryApi: LibraryApiService,
    private controlApi: ControlApiService
  ) {}

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading = true;
    this.error = '';
    forkJoin({ library: this.libraryApi.health(), control: this.controlApi.health() }).subscribe({
      next: () => {
        this.libraryOnline = true;
        this.controlOnline = true;
        this.loading = false;
      },
      error: () => {
        this.checkIndividually();
      }
    });
  }

  private checkIndividually(): void {
    let completed = 0;
    const done = () => {
      completed += 1;
      if (completed === 2) {
        this.loading = false;
        this.error = 'One or more services are unavailable.';
      }
    };
    this.libraryApi.health().subscribe({ next: () => { this.libraryOnline = true; done(); }, error: () => { this.libraryOnline = false; done(); } });
    this.controlApi.health().subscribe({ next: () => { this.controlOnline = true; done(); }, error: () => { this.controlOnline = false; done(); } });
  }
}
