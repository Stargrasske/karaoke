import { Type } from '@angular/core';

/** Registered widget kinds. */
export type WidgetType =
  | 'service-status'
  | 'now-playing'
  | 'playback'
  | 'queue'
  | 'library'
  | 'recording'
  | 'operations';

/** Rendering style variant. Extend as new presets are added (Phase 7) — never edit existing style components. */
export type WidgetStyle = 'ascii' | 'styled';

export interface WidgetSize {
  cols: number;
  rows: number;
  minCols?: number;
  minRows?: number;
}

/** A single placed widget within a dashboard layout. */
export interface WidgetInstance {
  id: string;
  type: WidgetType;
  style: WidgetStyle;
  x: number;
  y: number;
  cols: number;
  rows: number;
  config?: Record<string, unknown>;
}

export interface DashboardLayout {
  name: string;
  widgets: WidgetInstance[];
}

/** Contract every widget content component implements so the frame/shell can host it generically. */
export interface WidgetContentComponent {
  config?: Record<string, unknown>;
}

export interface WidgetRegistryEntry {
  type: WidgetType;
  style: WidgetStyle;
  label: string;
  component: Type<WidgetContentComponent>;
  defaultSize: WidgetSize;
}
