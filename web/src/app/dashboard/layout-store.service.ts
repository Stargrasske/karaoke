import { Injectable } from '@angular/core';

import { DashboardLayout, WidgetInstance } from './models/dashboard.types';

const STORAGE_KEY = 'karaoke.dashboards.v4';
const LEGACY_STORAGE_KEYS = ['karaoke.dashboards.v3', 'karaoke.dashboards.v2', 'karaoke.dashboards.v1'];
const ACTIVE_KEY = 'karaoke.dashboards.active';

const DEFAULT_LAYOUT: DashboardLayout = {
  name: 'Live Session',
  widgets: [
    { id: 'w-status', type: 'service-status', style: 'styled', x: 0, y: 0, cols: 2, rows: 2 },
    { id: 'w-now-playing', type: 'now-playing', style: 'styled', x: 2, y: 0, cols: 6, rows: 5 },
    { id: 'w-playback', type: 'playback', style: 'styled', x: 8, y: 0, cols: 4, rows: 3 },
    { id: 'w-queue', type: 'queue', style: 'ascii', x: 0, y: 5, cols: 12, rows: 4 }
  ]
};

const SONGBOOK_LAYOUT: DashboardLayout = {
  name: 'Songbook',
  widgets: [
    { id: 'w-songbook', type: 'library', style: 'styled', x: 0, y: 0, cols: 8, rows: 6 },
    { id: 'w-songbook-queue', type: 'queue', style: 'ascii', x: 8, y: 0, cols: 4, rows: 6 }
  ]
};

const SEEDED_LAYOUTS: DashboardLayout[] = [
  DEFAULT_LAYOUT,
  SONGBOOK_LAYOUT,
  {
    name: 'Capture Studio',
    widgets: [
      { id: 'w-recording-status', type: 'service-status', style: 'styled', x: 0, y: 0, cols: 3, rows: 2 },
      { id: 'w-recording', type: 'recording', style: 'styled', x: 3, y: 0, cols: 9, rows: 6 }
    ]
  },
  {
    name: 'System',
    widgets: [
      { id: 'w-operations-status', type: 'service-status', style: 'ascii', x: 0, y: 0, cols: 3, rows: 2 },
      { id: 'w-operations', type: 'operations', style: 'styled', x: 3, y: 0, cols: 9, rows: 6 }
    ]
  }
];

/** Persists named dashboard layouts to localStorage. Backend persistence is a Phase 7 stretch goal. */
@Injectable({ providedIn: 'root' })
export class LayoutStoreService {
  listDashboards(): DashboardLayout[] {
    const raw = localStorage.getItem(STORAGE_KEY)
      ?? LEGACY_STORAGE_KEYS.map((key) => localStorage.getItem(key)).find((value) => value !== null);
    if (!raw) {
      const seeded = SEEDED_LAYOUTS;
      this.saveAll(seeded);
      return seeded;
    }
    try {
      const parsed = JSON.parse(raw) as Array<DashboardLayout & { widgets: Array<WidgetInstance & { type: string }> }>;
      if (!Array.isArray(parsed) || !parsed.length) return [DEFAULT_LAYOUT];
      const typeMap: Record<string, WidgetInstance['type']> = {
        'placeholder-now-playing': 'now-playing',
        'placeholder-queue': 'queue',
        'placeholder-stats': 'service-status'
      };
      const migrated = parsed.map((dashboard) => ({
        ...dashboard,
        widgets: dashboard.widgets.map((widget) => ({
          ...widget,
          type: typeMap[widget.type] ?? widget.type
        })).filter((widget): widget is WidgetInstance =>
          ['service-status', 'now-playing', 'playback', 'queue', 'library', 'recording', 'operations'].includes(widget.type)
        )
      })).map((dashboard) => {
        if (dashboard.name === 'Player' || dashboard.name === 'Live Session') return DEFAULT_LAYOUT;
        if (dashboard.name === 'Recording') return SEEDED_LAYOUTS.find((layout) => layout.name === 'Capture Studio')!;
        if (dashboard.name === 'Operations') return SEEDED_LAYOUTS.find((layout) => layout.name === 'System')!;
        return dashboard;
      });
      if (!migrated.some((dashboard) => dashboard.name === SONGBOOK_LAYOUT.name)) {
        migrated.push(SONGBOOK_LAYOUT);
      }
      this.saveAll(migrated);
      return migrated;
    } catch {
      return [DEFAULT_LAYOUT];
    }
  }

  saveAll(dashboards: DashboardLayout[]): void {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(dashboards));
  }

  save(dashboard: DashboardLayout): void {
    const all = this.listDashboards();
    const idx = all.findIndex((d) => d.name === dashboard.name);
    if (idx >= 0) {
      all[idx] = dashboard;
    } else {
      all.push(dashboard);
    }
    this.saveAll(all);
  }

  delete(name: string): void {
    this.saveAll(this.listDashboards().filter((d) => d.name !== name));
  }

  rename(oldName: string, newName: string): void {
    const all = this.listDashboards();
    const dashboard = all.find((d) => d.name === oldName);
    if (dashboard) {
      dashboard.name = newName;
      this.saveAll(all);
    }
  }

  getActiveName(): string {
    const active = localStorage.getItem(ACTIVE_KEY);
    const renamed: Record<string, string> = {
      Player: 'Live Session',
      Recording: 'Capture Studio',
      Operations: 'System'
    };
    return (active && renamed[active]) || active || this.listDashboards()[0].name;
  }

  setActiveName(name: string): void {
    localStorage.setItem(ACTIVE_KEY, name);
  }

  updateWidgets(name: string, widgets: WidgetInstance[]): void {
    const all = this.listDashboards();
    const dashboard = all.find((d) => d.name === name);
    if (dashboard) {
      dashboard.widgets = widgets;
      this.saveAll(all);
    }
  }
}
