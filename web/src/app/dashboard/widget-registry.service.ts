import { Injectable } from '@angular/core';

import { LibraryComponent } from '../widgets/library/library.component';
import { NowPlayingComponent } from '../widgets/now-playing/now-playing.component';
import { OperationsComponent } from '../widgets/operations/operations.component';
import { PlaybackComponent } from '../widgets/playback/playback.component';
import { QueueComponent } from '../widgets/queue/queue.component';
import { RecordingComponent } from '../widgets/recording/recording.component';
import { ServiceStatusComponent } from '../widgets/service-status/service-status.component';
import { WidgetRegistryEntry, WidgetStyle, WidgetType } from './models/dashboard.types';

const ENTRIES: WidgetRegistryEntry[] = [
  {
    type: 'service-status',
    style: 'ascii',
    label: 'Rig Status',
    component: ServiceStatusComponent,
    defaultSize: { cols: 2, rows: 2, minCols: 2, minRows: 2 }
  },
  {
    type: 'service-status',
    style: 'styled',
    label: 'Rig Status',
    component: ServiceStatusComponent,
    defaultSize: { cols: 2, rows: 2, minCols: 2, minRows: 2 }
  },
  {
    type: 'now-playing',
    style: 'ascii',
    label: 'Stage Feed',
    component: NowPlayingComponent,
    defaultSize: { cols: 6, rows: 5, minCols: 4, minRows: 3 }
  },
  {
    type: 'now-playing',
    style: 'styled',
    label: 'Stage Feed',
    component: NowPlayingComponent,
    defaultSize: { cols: 6, rows: 5, minCols: 4, minRows: 3 }
  },
  {
    type: 'playback',
    style: 'ascii',
    label: 'Transport',
    component: PlaybackComponent,
    defaultSize: { cols: 6, rows: 3, minCols: 4, minRows: 3 }
  },
  {
    type: 'playback',
    style: 'styled',
    label: 'Transport',
    component: PlaybackComponent,
    defaultSize: { cols: 6, rows: 3, minCols: 4, minRows: 3 }
  },
  ...(['queue', 'library', 'recording', 'operations'] as const).flatMap((type) =>
    (['ascii', 'styled'] as const).map((style) => ({
      type,
      style,
      label: ({ queue: 'Up Next', library: 'Songbook', recording: 'Capture Deck', operations: 'Backstage' })[type],
      component: ({
        queue: QueueComponent,
        library: LibraryComponent,
        recording: RecordingComponent,
        operations: OperationsComponent
      })[type],
      defaultSize: ({
        queue: { cols: 5, rows: 4, minCols: 3, minRows: 3 },
        library: { cols: 7, rows: 4, minCols: 4, minRows: 3 },
        recording: { cols: 8, rows: 5, minCols: 5, minRows: 4 },
        operations: { cols: 8, rows: 5, minCols: 5, minRows: 4 }
      })[type]
    }))
  )
];

/** Maps (widgetType, style) to the component + default sizing that renders it. Register new widgets/styles here only. */
@Injectable({ providedIn: 'root' })
export class WidgetRegistryService {
  private readonly entries = ENTRIES;

  all(): WidgetRegistryEntry[] {
    return this.entries;
  }

  find(type: WidgetType, style: WidgetStyle): WidgetRegistryEntry | undefined {
    return this.entries.find((e) => e.type === type && e.style === style);
  }

  stylesFor(type: WidgetType): WidgetStyle[] {
    return this.entries.filter((e) => e.type === type).map((e) => e.style);
  }
}
