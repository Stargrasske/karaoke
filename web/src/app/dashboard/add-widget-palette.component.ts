import { Component, EventEmitter, Output } from '@angular/core';

import { WidgetInstance } from './models/dashboard.types';
import { WidgetRegistryService } from './widget-registry.service';

/** Lists every registered widget type/style combo; emits a new WidgetInstance to add to the active dashboard. */
@Component({
  selector: 'app-add-widget-palette',
  standalone: true,
  templateUrl: './add-widget-palette.component.html',
  styleUrl: './add-widget-palette.component.scss'
})
export class AddWidgetPaletteComponent {
  @Output() add = new EventEmitter<WidgetInstance>();

  constructor(private registry: WidgetRegistryService) {}

  get entries() {
    return this.registry.all();
  }

  onAdd(type: WidgetInstance['type'], style: WidgetInstance['style']): void {
    const entry = this.registry.find(type, style);
    if (!entry) {
      return;
    }
    this.add.emit({
      id: `w-${type}-${style}-${Date.now()}`,
      type,
      style,
      x: 0,
      y: 0,
      cols: entry.defaultSize.cols,
      rows: entry.defaultSize.rows
    });
  }
}
